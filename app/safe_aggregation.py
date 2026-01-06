from concurrent.futures import ProcessPoolExecutor, TimeoutError
import logging

logger = logging.getLogger("truth_engine")


def _run_pipeline(mpn, upc, title):
    from .aggregation import aggregate_product
    return aggregate_product(mpn=mpn, upc=upc, title=title)


def aggregate_product_safe(
    mpn: str = None,
    upc: str = None,
    title: str = None,
) -> dict:
    logger.info(f"SAFE aggregation started for {mpn or title}")

    try:
        with ProcessPoolExecutor(max_workers=5) as executor:
            future = executor.submit(_run_pipeline, mpn, upc, title)
            return future.result(timeout=600)

    except TimeoutError:
        logger.error("Pipeline exceeded 60 seconds — killed")
        return {
            "status": "timeout",
            "ready_for_publish": False,
            "error": "pipeline_timeout",
        }

    except Exception as e:
        logger.critical(f"Pipeline crashed: {e}")
        return {
            "status": "failed",
            "ready_for_publish": False,
            "error": str(e),
        }
