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

  validation {
    condition     = length(trimspace(var.ssh_key_name)) > 0
    error_message = "ssh_key_name must be a non-empty DigitalOcean SSH key name."
  }
}

variable "ssh_source_addresses" {
  type        = list(string)
  description = "List of IP addresses allowed to connect to the Droplet via SSH (Port 22)"
}
