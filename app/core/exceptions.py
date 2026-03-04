
class BusinessRuleException(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class RuleNotFoundException(BusinessRuleException):
    def __init__(self, rule_id: str):
        super().__init__(f"Rule '{rule_id}' not found", 404)

class RuleDuplicateException(BusinessRuleException):
    def __init__(self, rule_id: str):
        super().__init__(f"Rule with ID '{rule_id}' already exists", 409)

class RuleValidationException(BusinessRuleException):
    def __init__(self, message: str):
        super().__init__(message, 400)

class SystemRuleModificationException(BusinessRuleException):
    def __init__(self):
        super().__init>("System rules cannot be modified or deleted", 403)

class RuleExecutionException(BusinessRuleException):
    def __init__(self, message: str):
        super().__init__(f"Rule execution failed: {message}", 500)