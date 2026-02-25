# from typing import Dict, Optional, Coroutine
# import asyncio
# import logging
# logger = logging.getLogger('task_manager')


# class TaskManager:
#     def __init__(self):
#         self._tasks: Dict[str, asyncio.Task] = {}

#     def submit(self, task_id: str, coro: Coroutine) -> asyncio.Task:
#         if task_id in self._tasks:
#             existing = self._tasks[task_id]
#             if not existing.done():
#                 logger.warning(f"Task {task_id} is already running, skipping duplicate")
#                 return existing 

#         task = asyncio.create_task(
#             self._run_safe(task_id, coro), name=task_id
#         )
#         self._tasks[task_id] = task
#         task.add_done_callback(lambda t: self._on_complete(task_id, t))
#         logger.info(f"Task {task_id} submitted (active tasks: {self.active_count})")
#         return task

#     async def _run_safe(self, task_id: str, coro: Coroutine):
#         try:
#             return await coro
#         except asyncio.CancelledError:
#             logger.info(f"Task {task_id} was cancelled")
#             raise
#         except Exception as e:
#             logger.error(
#                 f"Task {task_id} failed with error :{e}", exc_info=True)

#     def _on_complete(self, task_id: str, task: asyncio.Task):
#         self._tasks.pop(task_id, None)
#         if task.cancelled():
#             logger.info(f"Task {task_id} cancelled")
#         elif task.exception():
#             logger.info(f"Task {task_id} failed:{task.exception()}")
#         else:
#             logger.info(f"Active tasks remaining:{self.active_count}")

#     def cancel(self, task_id: str) -> bool:
#         task = self._tasks.get(task_id)
#         if task and not task.done():
#             task.cancel()
#             logger.info(f"Task {task_id} cancellation requested")
#             return True
#         return False
    
#     def is_running(self,task_id:str)-> bool:
#         task=self._tasks.get(task_id)
#         return task is not None and not task.done()
#     @property
#     def active_count(self)->int:
#         self._tasks={
#             tid:t for tid, t in self._tasks.items()
#             if not t.done()
#         }
#         return len(self._tasks)
#     @property
#     def active_tasks(self) -> Dict[str, str]:
#         return {
#             tid: "running" if not t.done() else "done"
#             for tid, t in self._tasks.items()
#         }
# task_manager = TaskManager()
