# Step 15: list audit records stuck in a non-terminal state. The Lambda prints them; a CloudWatch metric
# filter and alarm on that line is the next step.
resource "aws_iam_role" "scheduler" {
  name = "${var.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.api.arn
    }]
  })
}

resource "aws_scheduler_schedule" "staleness" {
  name                = "${var.name}-staleness"
  schedule_expression = "rate(15 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.api.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "gwp.staleness" })
  }
}
