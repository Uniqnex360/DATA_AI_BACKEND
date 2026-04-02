from sqlalchemy.orm.attributes import flag_modified
from app.models.product import Product

def track_llm_usage(product: Product, llm_provider: str, is_enrichment_attempt: bool, logger) -> None:
    if not is_enrichment_attempt:
        logger.info(f"NOT tracking LLM {llm_provider} - is_enrichment_attempt=False (workflow_stage={product.workflow_stage})")
        return
    
    logger.info(f"Tracking LLM {llm_provider} for product {product.product_code}")
    
    if product.used_llms is None:
        product.used_llms = []
        logger.info(f"Initialized used_llms as empty list")
    
    if llm_provider not in product.used_llms:
        product.used_llms.append(llm_provider)
        flag_modified(product, "used_llms") 
        logger.info(f" Added {llm_provider} to used_llms. Now: {product.used_llms}")
    else:
        logger.info(f" {llm_provider} already in used_llms: {product.used_llms}")