# For the S3VectorsRetriever in src/gwp/retrieval.py (untested). Off by default. Argument names follow the
# provider docs for aws_s3vectors_vector_bucket and aws_s3vectors_index as read on 2026-09-25; confirm them
# with `terraform validate` before relying on this file.
resource "aws_s3vectors_vector_bucket" "policy" {
  count              = var.enable_s3_vectors ? 1 : 0
  vector_bucket_name = "${var.name}-policy-vectors"
}

resource "aws_s3vectors_index" "policy" {
  count              = var.enable_s3_vectors ? 1 : 0
  vector_bucket_name = aws_s3vectors_vector_bucket.policy[0].vector_bucket_name
  index_name         = "policy-chunks"
  data_type          = "float32"
  dimension          = 1024 # Titan Text Embeddings V2
  distance_metric    = "cosine"
}
