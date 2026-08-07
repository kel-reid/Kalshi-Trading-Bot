output "droplet_ip" {
  value       = digitalocean_droplet.bot_server.ipv4_address
  description = "The public IP address of the bot server droplet"
}

output "vpc_id" {
  value       = digitalocean_vpc.bot_vpc.id
  description = "The ID of the custom VPC created for network isolation"
}
