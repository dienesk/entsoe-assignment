variable "name_prefix" {
  type = string
}

variable "raw_response_expiration_days" {
  type = number
}

variable "tags" {
  type    = map(string)
  default = {}
}
