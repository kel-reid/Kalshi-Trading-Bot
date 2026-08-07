terraform {
  required_version = ">= 1.0.0"
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.0"
    }
  }
}

provider "digitalocean" {
  # Token is fetched from DIGITALOCEAN_TOKEN environment variable by default
}

# 1. Create a dedicated Virtual Private Cloud (VPC) for network isolation
resource "digitalocean_vpc" "bot_vpc" {
  name     = "kalshi-bot-vpc"
  region   = var.region
  ip_range = "10.10.0.0/16"
}

# 2. Setup firewall rules to protect the Droplet
resource "digitalocean_firewall" "bot_firewall" {
  name = "kalshi-bot-firewall"

  tags = ["kalshi-bot", "production"]

  # Allow inbound SSH traffic (Port 22)
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # Outbound traffic (allow all outbound so the bot can connect to Kalshi API and scrape metrics)
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

# 3. Reference or create SSH key for Droplet access
data "digitalocean_ssh_key" "deploy_key" {
  name = var.ssh_key_name
}

# 4. Provision the Droplet inside our custom VPC
resource "digitalocean_droplet" "bot_server" {
  image              = "docker-20-04" # Pre-configured with Docker and Compose
  name               = "kalshi-bot-prod"
  region             = var.region
  size               = var.droplet_size
  vpc_uuid           = digitalocean_vpc.bot_vpc.id
  ssh_keys           = [data.digitalocean_ssh_key.deploy_key.id]
  backups            = false
  monitoring         = true
  ipv6               = false
  resize_disk        = true

  tags = ["kalshi-bot", "production"]
}
