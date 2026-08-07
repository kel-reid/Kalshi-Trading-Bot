variable "region" {
  type        = string
  description = "DigitalOcean region to deploy resources in"
  default     = "nyc1"
}

variable "droplet_size" {
  type        = string
  description = "Droplet size slug"
  default     = "s-1vcpu-1gb" # Minimal/cheapest plan, perfectly sized for a single trading bot
}

variable "ssh_key_name" {
  type        = string
  description = "Name of the SSH key registered in DigitalOcean to associate with the Droplet"
}
