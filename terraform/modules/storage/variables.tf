variable "name_prefix" {
  type = string
}

variable "raw_json_expiration_days" {
  type = number
}

variable "tags" {
  type    = map(string)
  default = {}
}
