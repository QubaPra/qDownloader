import asyncio
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import core.state
from core.state import progress_queues, jobs, _remove_task_from_queue

router = APIRouter()

class SettingsRequest(BaseModel):
    parallel: bool

@router.post("/api/settings")
async def update_settings(req: SettingsRequest):
    core.state.PARALLEL_DOWNLOADS = req.parallel
    return {"status": "ok"}

async def sse_event(gen: asyncio.Queue):
    try:
        while True:
            item = await gen.get()
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("final"):
                break
    except asyncio.CancelledError:
        pass

@router.get("/api/progress/{job_id}")
async def get_progress(job_id: str):
    q = progress_queues.get(job_id)
    if q is None:
        q = asyncio.Queue()
        progress_queues[job_id] = q
    return StreamingResponse(sse_event(q), media_type="text/event-stream")

@router.delete("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Nie znaleziono zadania")

    if job.done or job.error:
        # Usuwamy zakończone lub błędne zadanie z pamięci żeby nie wracało po odświeżeniu
        jobs.pop(job_id, None)
        return {"status": "removed"}

    job.cancel = True
    if job.proc and job.proc.returncode is None:
        try:
            job.proc.terminate()
        except ProcessLookupError:
            pass
    else:
        removed = await _remove_task_from_queue(job_id)
        if removed:
            job.done = True
            q = progress_queues.setdefault(job_id, asyncio.Queue())
            await q.put({"type": "cancelled", "job_id": job_id, "final": True})
            print(f"[cancel] Usunięto z kolejki {job_id}")
            # po wyrzuceniu z kolejki, usuń zgłoszenie od razu
            jobs.pop(job_id, None)
            return {"status": "cancelled"}
    print(f"[cancel] Anulowano {job_id}")
    return {"status": "cancelling"}

@router.get("/api/state")
async def get_state():
    state_jobs = []
    # Kopiujemy klucze aby w razie usunięcia nie zepsuć iteracji
    for j_id in list(core.state.jobs.keys()):
        j = core.state.jobs.get(j_id)
        if not j or j.cancel:
            # Pomijamy anulowane jobs, one same dogasną lub już zgasły
            continue
        state_jobs.append({
            "id": j.id,
            "platform": j.platform,
            "meta": j.meta,
            "req_data": j.req_data,
            "info_text": j.info_text,
            "cancel": j.cancel,
            "done": j.done,
            "error": j.error,
            "last_progress": j.last_progress,
            "last_time": j.last_time
        })
    return {"jobs": state_jobs, "parallel": core.state.PARALLEL_DOWNLOADS}
