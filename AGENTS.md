# Rules for working on daolab
- The plan in plan/ is the source of truth. Precedence: root README > plan docs. plan/09 and plan/10 supersede 02-05 where they differ.
- Work on ONE task ID at a time. Read its row in plan/10-backend-tasksheet.md and the doc that owns the area.
- Only edit the files listed for that task. If another file must change, stop and ask.
- Never edit scheduler.py logic, engine.py, runner.py, ipam.py, access.py or health.py (Akshay owns them) unless the task row says so.
- Write the failing test first, then the code. Run `make test` after every change; never claim success without showing the output.
- Do not add new frameworks, queues, or dependencies unless the plan says so.
- All timestamps are UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`. JSON columns are touched only through models.py.
- Never commit .env, tokens or secrets. Never push; I will review and push.
- If the plan docs contradict each other or the code, stop and tell me instead of guessing.
