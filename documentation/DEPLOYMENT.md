# Deployment notes

How the Flask app in `app/` was set up on the Ubuntu 24.04 VM. The Azure resources have since been deleted, so these are notes rather than a one-click deploy.

Resource names used: resource group `TechStart-Solutions`, VM `techstart-vm`, Key Vault `techstart-kv`, storage account `techstartsolutionstorage`, blob container `app-files`, SQL server `techstartsolutions`, database `TechStartDB`.

## 1. Install packages on the VM

```bash
sudo apt update
sudo apt install -y python3-venv unixodbc-dev apache2 libapache2-mod-wsgi-py3
# Microsoft ODBC Driver 18 for SQL Server is also needed (same driver sqlcmd uses)
python3 -m venv venv
source venv/bin/activate
pip install -r app/requirements.txt
```

## 2. Give the VM's managed identity access to Key Vault and Storage

The app never holds credentials in code. It uses the VM's managed identity to read two secrets from Key Vault (`sql-connection-string` and `storage-connection-string`).

```bash
RG="TechStart-Solutions"
VM="techstart-vm"
KV="techstart-kv"
STG="techstartsolutionstorage"

VM_PID=$(az vm show -g "$RG" -n "$VM" --query identity.principalId -o tsv)
KV_ID=$(az keyvault show -g "$RG" -n "$KV" --query id -o tsv)
STG_ID=$(az storage account show -g "$RG" -n "$STG" --query id -o tsv)

az role assignment create --assignee-object-id "$VM_PID" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope "$KV_ID"

az role assignment create --assignee-object-id "$VM_PID" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "$STG_ID"
```

## 3. Create the two tables in Azure SQL

```sql
CREATE TABLE dbo.users (
  user_id INT IDENTITY(1,1) PRIMARY KEY,
  username NVARCHAR(50) NOT NULL UNIQUE,
  full_name NVARCHAR(100) NOT NULL,
  role NVARCHAR(20) NOT NULL CHECK (role IN ('developer','qa','pm')),
  password_hash NVARCHAR(255) NOT NULL,
  created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE dbo.tickets (
  ticket_id INT IDENTITY(1,1) PRIMARY KEY,
  title NVARCHAR(200) NOT NULL,
  description NVARCHAR(MAX) NULL,
  status NVARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','done')),
  created_by INT NOT NULL,
  created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
  attachment_blob NVARCHAR(260) NULL,
  CONSTRAINT FK_tickets_users FOREIGN KEY (created_by) REFERENCES dbo.users(user_id)
);
```

(`flask init-db` in `app.py` does the same thing.)

## 4. Add the six sample users

Passwords are stored as Werkzeug hashes, never in plain text. Generate a hash for each user:

```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('<password>'))"
```

Then insert them:

```sql
INSERT INTO dbo.users (username, full_name, role, password_hash) VALUES
('dev1','Dev One','developer','<hash>'),
('dev2','Dev Two','developer','<hash>'),
('qa1','QA One','qa','<hash>'),
('qa2','QA Two','qa','<hash>'),
('pm1','PM One','pm','<hash>'),
('pm2','PM Two','pm','<hash>');
```

## 5. Serve it through Apache and mod_wsgi

- Copy the contents of `app/` to `/var/www/flaskapp` and rename `app.wsgi` to `flaskapp.wsgi` (the name the Apache config points to).
- `sudo chown -R www-data:www-data /var/www/flaskapp`
- Create a self-signed certificate in `/etc/apache2/ssl/` (`techstart.crt` and `techstart.key`).
- Copy the two site files from `config/etc/apache2/sites-available/`, set `ServerName` and a random `FLASK_SECRET_KEY` (for example `python3 -c 'import secrets; print(secrets.token_hex(32))'`).
- Enable everything and reload:

```bash
sudo a2enmod ssl rewrite wsgi
sudo a2ensite flaskapp.conf flaskapp-ssl.conf
sudo systemctl reload apache2
```

## Quick local test (without Apache)

```bash
export KEY_VAULT_NAME="techstart-kv"
export BLOB_CONTAINER_NAME="app-files"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
source venv/bin/activate
python3 app/app.py
```

Run directly like this, the app listens on port 5000. That port isn't open in the NSG or UFW, so it can't be reached from outside, and I tested it from my laptop through an SSH tunnel instead:

```bash
ssh -i techstart-vm_key.pem -L 5000:127.0.0.1:5000 azureuser@<vm-public-ip>
```
