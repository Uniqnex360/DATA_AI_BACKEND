
from enum import Enum

class RuleCategory(str, Enum):
  ENRICHMENT = "enrichment"
  AGGREGATION = "aggregation"
  VALIDATION = "validation"
  CLEANSING = "cleansing"

class RuleStatus(str, Enum):
  ACTIVE = "active"
  INACTIVE = "inactive"
#   DRAFT = "draft"