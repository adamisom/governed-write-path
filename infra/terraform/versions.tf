# Written for the spike and never applied. `terraform init`, `validate` and `plan` have not been run:
# Terraform is not installed on the machine this was written on. Run `terraform fmt` and `validate` first.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.25" # S3 Vectors resources; check the provider changelog before the first plan
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project = "governed-write-path"
    }
  }
}
