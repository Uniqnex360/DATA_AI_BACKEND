import asyncio
import logging
from typing import Dict, Any, Optional
from asyncio import Queue


logger = logging.getLogger("worker_pool")

class ProductWorkerPool:
    def __init__(self, process_function, worker_count: int = 1):
        self.process_function = process_function
        self.worker_count = worker_count
        self.queue: Queue = Queue()
        self.workers_started = False
        self.workers = []
    
    async def _worker(self, worker_id: int):
        logger.info(f" Worker {worker_id} started")
        
        while True:
            try:
                task_data = await self.queue.get()
                
                if task_data is None:
                    break
                
                product_id = task_data['product_id']
                llm_provider=task_data['llm_provider']
                missing_llm_provider = task_data.get('missing_llm_provider')
                logger.info(f" Worker {worker_id} processing {product_id} with LLM: {llm_provider}")
                
                await self.process_function(product_id, llm_provider, missing_llm_provider)
                
                logger.info(f" Worker {worker_id} completed {product_id}")
                
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}", exc_info=True)
            finally:
                self.queue.task_done()
                await asyncio.sleep(10)  
    
    async def start(self):
        if self.workers_started:
            return
        
        for i in range(self.worker_count):
            worker = asyncio.create_task(self._worker(i))
            self.workers.append(worker)
        
        self.workers_started = True
        logger.info(f" Started {self.worker_count} workers")
    
    async def submit(self, product_id: str, llm_provider: str = 'openai', missing_llm_provider: Optional[str] = None) -> int:
        await self.start()
        await self.queue.put({'product_id': product_id, 'llm_provider': llm_provider, 'missing_llm_provider': missing_llm_provider})
        return self.queue.qsize()
    
    def get_status(self) -> Dict[str, Any]:
        return {
            'workers_active': self.worker_count if self.workers_started else 0,
            'queue_size': self.queue.qsize(),
        }

_worker_pool = None

def get_worker_pool(process_function=None):
    global _worker_pool
    if _worker_pool is None:
        if process_function is None:
            raise ValueError("process_function required")
        _worker_pool = ProductWorkerPool(process_function, worker_count=2)
    return _worker_pool