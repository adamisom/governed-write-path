variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "gwp"
}

variable "lambda_zip" {
  description = "Path to the Lambda package built by infra/build_lambda.sh."
  type        = string
  default     = "../build/lambda.zip"
}

variable "model_provider" {
  description = "bedrock or anthropic. With anthropic, the API key must be supplied separately."
  type        = string
  default     = "bedrock"
}

variable "reader_model" {
  description = "Model id for the quarantined reader. Check the Bedrock model card for the exact inference profile id."
  type        = string
  default     = "global.anthropic.claude-haiku-4-5"
}

variable "proposer_model" {
  type    = string
  default = "global.anthropic.claude-sonnet-5"
}

variable "api_keys_json" {
  description = "JSON map of sha256(api key) to {principal_id, role, tenant_id}. One key per role in v0."
  type        = string
  sensitive   = true
}

variable "monthly_budget_usd" {
  description = "Budget alarm threshold. Set this before anything else is deployed."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  type = string
}

variable "enable_s3_vectors" {
  description = "Create the S3 Vectors bucket and index for the embeddings retriever (weekend 3)."
  type        = bool
  default     = false
}
