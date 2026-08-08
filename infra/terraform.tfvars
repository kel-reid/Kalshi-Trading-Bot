# Name of the SSH key registered in your DigitalOcean account
ssh_key_name = "kalshi-deploy-key"

# List of IP addresses allowed to SSH into the Droplet.
# For security, restrict this to your public IP (e.g. ["73.124.56.78/32"]).
# Using ["0.0.0.0/0", "::/0"] allows connections from any IP.
ssh_source_addresses = ["0.0.0.0/0", "::/0"]
