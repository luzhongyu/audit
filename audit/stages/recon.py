"""Stage 1: Recon — map the repo, emit initial hunt tasks.

Task count is dynamically scaled to repo size and filtered by detected
tech stack to prevent token waste on tiny codebases or impossible attack
classes (see [[audit-pipeline-token-optimization]]).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from audit.runner import run_agent
from audit.state import StateDB
from audit.stages._common import StageContext, count_source_files

log = logging.getLogger(__name__)

# Upper bound — small repos get fewer tasks (see _dynamic_max_tasks).
DEFAULT_MAX_TASKS = 80

# ------------------------------------------------------------------
# Tech-stack detection — attack-class relevance signals
# ------------------------------------------------------------------

# Map build-file names to a human label.
_BUILD_FILE_NAMES = [
    "pom.xml", "build.gradle", "build.gradle.kts",
    "package.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock",
    "Gemfile", "Gemfile.lock",
    "CMakeLists.txt", "Makefile",
]

# Attack classes that require specific dependencies or code patterns to exist.
# Each entry: (attack_class, dependency_signal_patterns, code_signal_patterns)
# If NONE of the patterns match, the attack class is flagged as "unlikely".
_ATTACK_CLASS_SIGNALS: list[tuple[str, list[str], list[str]]] = [
    # ── database ──
    ("sql_injection", [
        r"(?i)(mysql|postgresql|sqlite|h2database|oracle|mssql|jdbc|jpa|hibernate|mybatis|sqlalchemy|psycopg|aiosqlite|asyncpg|pgx|go-sqlite3|pq|mariadb)",
    ], [
        r"(?i)(DriverManager\.getConnection|createEntityManager|SessionFactory|sqlalchemy\.create_engine|psycopg2\.connect|sqlite3\.connect|database/sql\.Open)",
    ]),
    ("nosql_injection", [
        r"(?i)(mongodb|mongo-java|spring-boot-starter-data-mongodb|mongoose|pymongo|motor|redis|jedis|lettuce|redisson|go-redis)",
    ], [
        r"(?i)(MongoClient|MongoCollection|mongoose\.connect|pymongo\.MongoClient|redis\.Redis|redis\.StrictRedis)",
    ]),
    # ── command execution ──
    ("command_injection", [
        # No specific dependency — detected via code patterns only
    ], [
        r"(?i)(Runtime\.exec|ProcessBuilder|\.exec\(|os\.system|subprocess\.(call|run|Popen|check_output)|shell_exec|exec\.Command|popen|command\(\)|shelljs|child_process\.exec)",
    ]),
    # ── XML ──
    ("xxe", [
        r"(?i)(xml-apis|xerces|woodstox|aalto-xml|jaxb-ri|dom4j|jdom|defusedxml|lxml)",
    ], [
        r"(?i)(DocumentBuilderFactory|SAXParser|XMLReader|XMLInputFactory|etree\.(parse|fromstring|iterparse)|lxml\.etree|xml2js\.parseString|defusedxml\.(parse|fromstring))",
    ]),
    # ── HTTP client (SSRF) ──
    ("ssrf", [
        r"(?i)(httpclient|okhttp|retrofit|resttemplate|webclient|feign|requests|urllib3|httpx|got|axios|node-fetch|reqwest|ureq)",
    ], [
        r"(?i)(HttpURLConnection|HttpClient\.newHttpClient|RestTemplate|WebClient|requests\.(get|post|put|delete|head)|urllib\.request|httpx\.(get|post)|axios\.(get|post)|fetch\(|reqwest::)",
    ]),
    # ── YAML deserialization ──
    ("deserialization_yaml", [
        r"(?i)(snakeyaml|pyyaml|ruamel\.yaml|js-yaml|yaml\.load)",
    ], [
        r"(?i)(Yaml\(\)|new Yaml\(|yaml\.load\(|yaml\.load_all\(|yaml\.loadAs|js-yaml\.load|ruamel\.yaml\.load)",
    ]),
    # ── pickle / native deserialization ──
    ("deserialization_pickle", [
        # Generic — most languages have native deserialization
    ], [
        r"(?i)(ObjectInputStream|readObject\(\)|pickle\.(loads|load)|cPickle\.(loads|load)|dill\.(loads|load)|unserialize\(|jsonpickle\.decode)",
    ]),
    # ── SSTI ──
    ("ssti", [
        r"(?i)(thymeleaf|jsp|freemarker|velocity|mustache|handlebars|jinja2|jinja|pug|ejs|nunjucks|liquid|tera|askama)",
    ], [
        r"(?i)(TemplateEngine|thymeleaf|FreeMarker|Velocity|Jinja2|jinja2\.Environment|render_template|pug\.(render|compile)|ejs\.(render|compile))",
    ]),
    # ── path traversal / file operations ──
    ("path_traversal", [
        # File I/O is near-universal — code patterns carry more signal
    ], [
        r"(?i)(java\.io\.File|java\.nio\.file\.(Files|Path)|new FileInputStream|new FileOutputStream|open\(\s*['\"]|fs\.(readFile|writeFile|createReadStream|createWriteStream)|os\.(open|rename|remove|mkdir))",
    ]),
    # ── LDAP ──
    ("ldap_injection", [
        r"(?i)(ldap|jndi|ldap3|python-ldap)",
    ], [
        r"(?i)(InitialDirContext|DirContext|LdapContext|ldap\.search|ldap3\.Connection)",
    ]),
    # ── XPath ──
    ("xpath_injection", [
        r"(?i)(xpath|javax\.xml\.xpath)",
    ], [
        r"(?i)(XPathFactory|XPath\.(compile|evaluate)|xpath\.(select|find)|etree\.XPath)",
    ]),
    # ── logging ──
    ("log_injection", [
        r"(?i)(log4j|slf4j|logback|log4net|winston|bunyan|pino)",
    ], [
        r"(?i)(log(ger)?\.(info|warn|error|debug|trace|fatal)|LOG\.(info|warn|error|debug)|console\.(log|warn|error)|logging\.(info|warn|error|debug))",
    ]),
]


def _find_build_file(repo_path: Path) -> str | None:
    """Return the relative path of the first build file found, or None."""
    for name in _BUILD_FILE_NAMES:
        candidate = repo_path / name
        if candidate.exists():
            return name
    return None


def _read_head(repo_path: Path, rel_path: str, lines: int = 80) -> str | None:
    """Read the first *lines* lines of a tracked file, or None."""
    fpath = repo_path / rel_path
    try:
        text = fpath.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return "\n".join(text.splitlines()[:lines])


def _detect_tech_stack(repo_path: Path) -> dict:
    """Scan build files and source code for attack-surface-relevant signals.

    Returns a compact dict designed for the recon LLM's ``user_input``:
        {
          "build_system": "maven" | "gradle" | "npm" | … | null,
          "build_file_snippet": "<first 80 lines of build file>",
          "likely_classes": ["idor", "auth_bypass", …],
          "unlikely_classes": ["sql_injection", "xxe", …],
          "notes": "<human-readable summary>",
        }

    "Likely" means the attack class has matching dependencies or code
    patterns.  "Unlikely" means zero signals were found.  Classes that
    are architectural (idor, auth_bypass, race_condition, logic_chain)
    are not classified either way — they are always possible.
    """
    # Always-possible classes (architectural, not dependency-driven).
    _ALWAYS_POSSIBLE = frozenset({
        "idor", "auth_bypass", "race_condition_toctou",
        "logic_chain", "mass_assignment", "xss_reflected",
        "xss_stored", "open_redirect", "information_exposure",
        "integer_overflow", "use_after_free",
    })

    build_file = _find_build_file(repo_path)
    build_snippet = _read_head(repo_path, build_file) if build_file else None

    likely: list[str] = []
    unlikely: list[str] = []

    for attack_class, dep_patterns, code_patterns in _ATTACK_CLASS_SIGNALS:
        if attack_class in _ALWAYS_POSSIBLE:
            continue

        found = False

        # 1. Check dependency patterns in build file.
        if build_snippet and dep_patterns:
            for pat in dep_patterns:
                if re.search(pat, build_snippet):
                    found = True
                    break

        # 2. Quick grep in source files (only if not found via deps).
        if not found and code_patterns:
            for pat in code_patterns:
                try:
                    result = subprocess.run(
                        ["grep", "-r", "-l", "-E", "--include=*.java",
                         "--include=*.py", "--include=*.js", "--include=*.ts",
                         "--include=*.go", "--include=*.rs", "--include=*.rb",
                         "--include=*.php",
                         pat, str(repo_path)],
                        capture_output=True, text=True, timeout=15,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        found = True
                        break
                except (subprocess.TimeoutExpired, OSError):
                    pass

        if found:
            likely.append(attack_class)
        else:
            unlikely.append(attack_class)

    # Build human-readable notes.
    notes_parts: list[str] = []
    if build_file:
        notes_parts.append(f"build: {build_file}")
    if unlikely:
        notes_parts.append(
            f"no signals for: {', '.join(sorted(unlikely))}"
        )
    if likely:
        notes_parts.append(
            f"signals found for: {', '.join(sorted(likely))}"
        )

    return {
        "build_system": build_file,
        "build_file_snippet": build_snippet,
        "likely_classes": sorted(likely),
        "unlikely_classes": sorted(unlikely),
        "notes": "; ".join(notes_parts) if notes_parts else "no build file found",
    }


# ------------------------------------------------------------------
# Source-file counting
# ------------------------------------------------------------------


def _dynamic_max_tasks(file_count: int,
                       explicit_max: int | None = None) -> tuple[int, int]:
    """Return ``(max_tasks, file_count)``.

    If *explicit_max* is provided (CLI override), it is used verbatim.
    Otherwise the ceiling is scaled to repository size:

    ============  ==========
    Source files  max_tasks
    ============  ==========
    < 10          5
    10 – 49       15
    50 – 199      40
    200+          80
    ============  ==========
    """
    if explicit_max is not None:
        return explicit_max, file_count
    if file_count < 10:
        return 5, file_count
    if file_count < 50:
        return 15, file_count
    if file_count < 200:
        return 40, file_count
    return 80, file_count


# ------------------------------------------------------------------
# Stage entry point
# ------------------------------------------------------------------


async def run_recon(
    ctx: StageContext,
    db: StateDB,
    max_tasks: int | None = None,
) -> dict:
    if db.get_recon_output(ctx.run_id) is not None:
        log.info("[%s] recon already complete, skipping", ctx.run_id)
        return db.get_recon_output(ctx.run_id)  # type: ignore[return-value]

    file_count = ctx.file_count or count_source_files(
        ctx.repo_path, config=ctx.config.file_count,
    )
    max_tasks, file_count = _dynamic_max_tasks(file_count, max_tasks)
    tech_stack = _detect_tech_stack(ctx.repo_path)

    sc = ctx.stage("recon")
    log.info(
        "[%s] recon: model=%s source_files=%d max_tasks=%d build=%s unlikely=%s",
        ctx.run_id, sc.model, file_count, max_tasks,
        tech_stack.get("build_system") or "none",
        ", ".join(tech_stack.get("unlikely_classes", [])),
    )

    result = await run_agent(
        stage="recon",
        prompt_file=ctx.prompt("01-recon"),
        user_input={
            "repo_path": str(ctx.repo_path),
            "max_tasks": max_tasks,
            "source_file_count": file_count,
            "tech_stack": tech_stack,
            **ctx.extras(),
        },
        schema_file=ctx.schema("recon_output"),
        allowed_tools=sc.tools,
        model=sc.model,
        cwd=ctx.repo_path,
        add_dirs=[ctx.repo_path],
        max_turns=sc.max_turns,
        permission_mode=sc.permission_mode,
        artifact_dir=ctx.results_dir("recon"),
        artifact_name="recon",
        repair_attempts=sc.repair_attempts,
    )

    payload = result.payload
    db.save_recon_output(ctx.run_id, payload)
    db.record_cost(ctx.run_id, "recon", None, result.raw_result_message)
    db.add_artifact(ctx.run_id, "recon", None, "jsonl", str(result.artifact_path))

    for task in payload.get("initial_tasks", []):
        task.setdefault("source", "recon")
        db.add_task(ctx.run_id, task)

    emitted = len(payload.get("initial_tasks", []))
    if emitted > max_tasks:
        log.warning(
            "[%s] recon emitted %d tasks but max_tasks=%d — model ignored the limit",
            ctx.run_id, emitted, max_tasks,
        )

    # Warn if model emitted tasks for unlikely attack classes.
    unlikely = set(tech_stack.get("unlikely_classes", []))
    if unlikely and emitted:
        unlikely_emitted = [
            t.get("attack_class") for t in payload.get("initial_tasks", [])
            if t.get("attack_class") in unlikely
        ]
        if unlikely_emitted:
            log.warning(
                "[%s] recon emitted %d task(s) for unlikely classes: %s",
                ctx.run_id, len(unlikely_emitted), ", ".join(sorted(set(unlikely_emitted))),
            )

    log.info(
        "[%s] recon done: subsystems=%d entry_points=%d initial_tasks=%d cost=$%.4f",
        ctx.run_id,
        len(payload.get("subsystems", [])),
        len(payload.get("architecture", {}).get("entry_points", [])),
        emitted,
        result.cost_usd or 0.0,
    )
    return payload
