# The system of record. Keys match gwp/store.py: pk and sk on both tables, a sparse index on the records
# table for runs that are not finalized, and a sparse index on the audit table for the staleness check.
resource "aws_dynamodb_table" "records" {
  name                        = "${var.name}-records"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "lease_flag"
    type = "S"
  }
  attribute {
    name = "lease_until"
    type = "S"
  }

  # A run carries lease_flag and lease_until until it is finalized. The scheduled sweep resumes a run whose
  # lease ran out, e.g. because its worker hit the Lambda timeout after claiming it.
  global_secondary_index {
    name            = "leased_runs"
    hash_key        = "lease_flag"
    range_key       = "lease_until"
    projection_type = "ALL"
  }

  # Only access records carry expires_at (epoch seconds, gwp/access.py); DynamoDB deletes each one after it.
  # Deletion by TTL needs no DeleteItem grant for the Lambda role.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "audit" {
  name                        = "${var.name}-audit"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "open_flag"
    type = "S"
  }
  attribute {
    name = "open_since"
    type = "S"
  }

  global_secondary_index {
    name            = "open_by_age"
    hash_key        = "open_flag"
    range_key       = "open_since"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
}

# Uploaded documents, keyed by tenant and content hash.
resource "aws_s3_bucket" "documents" {
  bucket_prefix = "${var.name}-documents-"
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
