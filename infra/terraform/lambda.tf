resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.name}-api"
  retention_in_days = 14
}

resource "aws_iam_role" "lambda" {
  name = "${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least privilege. There is no DeleteItem on either table: nothing in the write path deletes, and a
# revert is a compensating write. TransactWriteItems needs the per-item actions it performs.
resource "aws_iam_role_policy" "lambda" {
  name = "${var.name}-lambda"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.api.arn}:*"
      },
      {
        Sid    = "Records"
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
        "dynamodb:ConditionCheckItem"]
        Resource = [aws_dynamodb_table.records.arn]
      },
      {
        Sid      = "Audit"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.audit.arn, "${aws_dynamodb_table.audit.arn}/index/open_by_age"]
      },
      {
        Sid      = "Documents"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.documents.arn}/*"
      },
      {
        Sid    = "Models"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # Narrow this to the two inference profiles and their foundation models once the ids are confirmed.
        Resource = ["arn:aws:bedrock:*::foundation-model/anthropic.*", "arn:aws:bedrock:*:*:inference-profile/*"]
      },
      ], var.enable_s3_vectors ? [{
        Sid      = "Vectors"
        Effect   = "Allow"
        Action   = ["s3vectors:QueryVectors", "s3vectors:GetVectors"]
        Resource = [aws_s3vectors_index.policy[0].index_arn]
    }] : [])
  })
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.name}-api"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "gwp.api.handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  memory_size      = 1024
  # Reader budget 30 s and proposer 60 s, each with one retry and backoff, plus the writes.
  timeout = 240
  # A hard cap on concurrent model calls, so a public endpoint can't run up a bill quickly.
  reserved_concurrent_executions = 2

  environment {
    variables = {
      GWP_RECORDS_TABLE   = aws_dynamodb_table.records.name
      GWP_AUDIT_TABLE     = aws_dynamodb_table.audit.name
      GWP_DOCUMENT_BUCKET = aws_s3_bucket.documents.bucket
      GWP_MODEL_PROVIDER  = var.model_provider
      GWP_READER_MODEL    = var.reader_model
      GWP_PROPOSER_MODEL  = var.proposer_model
      GWP_API_KEYS        = var.api_keys_json # move to Secrets Manager before this holds real keys
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
}
