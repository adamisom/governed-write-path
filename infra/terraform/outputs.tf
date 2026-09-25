output "api_url" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "records_table" {
  value = aws_dynamodb_table.records.name
}

output "audit_table" {
  value = aws_dynamodb_table.audit.name
}

output "document_bucket" {
  value = aws_s3_bucket.documents.bucket
}
