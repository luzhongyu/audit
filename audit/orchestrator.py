"""Pipeline driver: Recon → (Hunt → Validate → Gapfill)* → Dedupe → Trace
                  → Feedback → (Hunt → Validate → Dedupe → Trace)* → Report
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from audit import stages
from audit.config import HarnessConfig
from audit.runner import QuotaExhaustedError
from audit.state import StateDB
from audit.stages._common import StageContext

log = logging.getLogger(__name__)


class CostExceeded(RuntimeError):
    pass


async def run_pipeline(
    *,
    repo_path: Path,
    run_id: str,
    db: StateDB,
    config: HarnessConfig,
    max_cost_usd: float | None = None,
    resume: bool = False,
    max_recon_tasks: int | None = None,
    live_target: dict | None = None,
    scope_notes: str | None = None,
) -> Path:
    ctx = StageContext(
        run_id=run_id,
        repo_path=repo_path.resolve(),
        config=config,
        live_target=live_target,
        scope_notes=scope_notes,
    )

    if db.get_run(run_id) is None:
        db.create_run(str(repo_path.resolve()), run_id)
        log.info(
            "[%s] starting fresh pipeline run against %s (gapfill_iters=%d feedback_iters=%d max_cost=$%s)",
            run_id, repo_path, config.gapfill_iterations,
            config.feedback_iterations,
            f"{max_cost_usd:.2f}" if max_cost_usd else "unlimited",
        )
    elif resume:
        # Flip status back to 'running' so subsequent /status calls don't
        # report a stale 'aborted'/'failed' while resume work is ongoing.
        db._conn.execute(  # type: ignore[attr-defined]
            "UPDATE runs SET status = 'running', finished_at = NULL WHERE run_id = ?",
            (run_id,),
        )
        db._conn.commit()  # type: ignore[attr-defined]
        # Re-queue any task left 'running' (interrupted mid-flight by a quota
        # abort or crash) or 'failed' (transient/quota error) so resume
        # actually re-attempts the incomplete work instead of skipping it —
        # Hunt only dispatches 'pending' tasks.
        requeued = db.reset_incomplete_tasks(run_id)
        if requeued:
            log.info("[%s] resume: re-queued %d interrupted/failed tasks", run_id, requeued)
        log.info("[%s] resuming existing run", run_id)
    else:
        raise RuntimeError(
            f"run_id {run_id!r} already exists; pass --resume to continue it."
        )

    def _budget_check(stage_name: str) -> None:
        if max_cost_usd is None:
            return
        spent = db.total_cost(run_id)
        if spent >= max_cost_usd:
            raise CostExceeded(
                f"[{run_id}] budget exhausted before {stage_name}: "
                f"${spent:.4f} >= ${max_cost_usd:.4f}"
            )

    try:
        pipeline_start = time.time()

        # ---- Stage 1: Recon ----
        _budget_check("recon")
        t0 = time.time()
        recon_kwargs = {} if max_recon_tasks is None else {"max_tasks": max_recon_tasks}
        await stages.run_recon(ctx, db, **recon_kwargs)
        log.info("[%s] ----- stage recon done in %.0fs -----", run_id, time.time() - t0)

        # ---- Stages 2-3-4 loop: Hunt → Validate → Gapfill ----
        for i in range(config.gapfill_iterations + 1):
            _budget_check(f"hunt(iter={i})")
            t0 = time.time()
            findings_added = await stages.run_hunt(ctx, db, budget_check=_budget_check)
            log.info("[%s] ----- stage hunt loop %d done in %.0fs -----", run_id, i, time.time() - t0)
            if findings_added == 0 and i > 0:
                log.info("[%s] no new findings — exiting Hunt/Gapfill loop", run_id)
                break

            _budget_check(f"validate(iter={i})")
            t0 = time.time()
            await stages.run_validate(ctx, db)
            log.info("[%s] ----- stage validate loop %d done in %.0fs -----", run_id, i, time.time() - t0)

            if i >= config.gapfill_iterations:
                break  # final iteration: don't gapfill again
            _budget_check(f"gapfill(iter={i})")
            t0 = time.time()
            new_tasks = await stages.run_gapfill(ctx, db)
            log.info("[%s] ----- stage gapfill loop %d done in %.0fs -----", run_id, i, time.time() - t0)
            if new_tasks == 0:
                log.info("[%s] gapfill produced 0 tasks — exiting loop", run_id)
                break

        # ---- Stage 5: Dedupe ----
        _budget_check("dedupe")
        t0 = time.time()
        await stages.run_dedupe(ctx, db)
        log.info("[%s] ----- stage dedupe done in %.0fs -----", run_id, time.time() - t0)

        # ---- Stage 6: Trace ----
        _budget_check("trace")
        t0 = time.time()
        await stages.run_trace(ctx, db)
        log.info("[%s] ----- stage trace done in %.0fs -----", run_id, time.time() - t0)

        # ---- Stage 7: Feedback (re-runs Hunt/Validate/Dedupe/Trace) ----
        for i in range(config.feedback_iterations):
            _budget_check(f"feedback(iter={i})")
            t0 = time.time()
            new_tasks = await stages.run_feedback(ctx, db)
            log.info("[%s] ----- stage feedback loop %d done in %.0fs -----", run_id, i, time.time() - t0)
            if new_tasks == 0:
                break
            _budget_check(f"feedback-hunt(iter={i})")
            t0 = time.time()
            await stages.run_hunt(ctx, db)
            log.info("[%s] ----- stage feedback-hunt loop %d done in %.0fs -----", run_id, i, time.time() - t0)
            _budget_check(f"feedback-validate(iter={i})")
            t0 = time.time()
            await stages.run_validate(ctx, db)
            log.info("[%s] ----- stage feedback-validate loop %d done in %.0fs -----", run_id, i, time.time() - t0)
            _budget_check(f"feedback-dedupe(iter={i})")
            t0 = time.time()
            await stages.run_dedupe(ctx, db)
            log.info("[%s] ----- stage feedback-dedupe loop %d done in %.0fs -----", run_id, i, time.time() - t0)
            _budget_check(f"feedback-trace(iter={i})")
            t0 = time.time()
            await stages.run_trace(ctx, db)
            log.info("[%s] ----- stage feedback-trace loop %d done in %.0fs -----", run_id, i, time.time() - t0)

        # ---- Stage 8: Report ----
        _budget_check("report")
        t0 = time.time()
        report_path = await stages.run_report(ctx, db)
        log.info("[%s] ----- stage report done in %.0fs -----", run_id, time.time() - t0)

        db.finish_run(run_id, "completed")
        log.info(
            "[%s] pipeline complete in %.0fs: total cost $%.4f — report at %s",
            run_id, time.time() - pipeline_start, db.total_cost(run_id), report_path,
        )
        return report_path

    except CostExceeded as e:
        log.error(str(e))
        db.finish_run(run_id, "aborted")
        raise
    except QuotaExhaustedError as e:
        # LLM provider quota exhausted — surface clearly; user should
        # wait and resume. Run is resumable via --resume once quota
        # returns.
        log.error(
            "[%s] quota exhausted — aborting (resumable with --resume): %s",
            run_id, str(e)[:300],
        )
        db.finish_run(run_id, "aborted")
        raise
    except Exception:
        db.finish_run(run_id, "failed")
        raise
