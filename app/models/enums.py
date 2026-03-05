
from enum import Enum

class RuleCategory(str, Enum):
  ENRICHMENT = "enrichment"
  AGGREGATION = "aggregation",
  EXTRACTION='extraction'
  STANDARDIZATION='standardization'

class RuleStatus(str, Enum):
  ACTIVE = "active"
  INACTIVE = "inactive"
#   DRAFT = "draft"