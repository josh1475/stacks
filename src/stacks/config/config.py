import os
import threading
import logging
import yaml
import copy
from stacks.constants import CONFIG_FILE, CONFIG_SCHEMA_FILE, RE_SECRET_KEY
from stacks.config.validate import _validate, ensure_login_credentials

logger = logging.getLogger('config')

# Settings the environment can set over config.yaml (for containers whose secrets come from an .env file).
# They are read on every get(), never written to config.yaml, and never shown by /api/config.
ENV_OVERRIDES = {
    ("catalogue", "only"): ("CATALOGUE_ONLY", "bool"),
    ("api", "key"): ("ADMIN_API_KEY", "secret"),
    ("api", "downloader_key"): ("DOWNLOADER_API_KEY", "secret"),
}


def env_override(keys):
    """(True, value) when the environment sets the setting at KEYS, else (False, None)."""
    spec = ENV_OVERRIDES.get(tuple(keys))
    if not spec:
        return False, None
    name, kind = spec
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return False, None
    raw = raw.strip()
    if kind == "bool":
        if raw.lower() in ("1", "true", "yes", "on"):
            return True, True
        if raw.lower() in ("0", "false", "no", "off"):
            return True, False
        logger.warning(f"{name} should be true or false; ignored")
        return False, None
    if RE_SECRET_KEY.fullmatch(raw):
        return True, raw
    logger.warning(f"{name} isn't a valid key (32 letters, digits, - or _); ignored")
    return False, None


class Config:
    """Configuration loader with live update support"""

    def __init__(self, config_path=CONFIG_FILE, schema_path=CONFIG_SCHEMA_FILE):
        self.config_path = config_path
        self.schema_path = schema_path
        self.lock = threading.Lock()

        self.load_schema()
        self.load()

        olddata = copy.deepcopy(self.data)
        self.data = self.validate(self.data, self.schema)
        self.ensure_login_credentials()
        if not self.data == olddata:
            logger.info("Some value(s) in config file did not conform to standard. Logfile has been healed.")
            self.save()
        

    def load(self):
        """Load configuration from file, or create empty dict."""
        with self.lock:
            try:
                with open(self.config_path, "r") as f:
                    self.data = yaml.safe_load(f) or {}
                    logger.debug("Loaded config.")
            except FileNotFoundError:
                logger.debug("No config found, seeding empty config for population.")
                self.data = {}

    def load_schema(self):
        """Load schema from file."""
        with self.lock:
            with open(self.schema_path, "r") as f:
                self.schema = yaml.safe_load(f)
                logger.debug("Loaded config schema.")

    def save(self):
        """Save configuration to file."""
        with self.lock:
            with open(self.config_path, "w") as f:
                yaml.dump(self.data, f, default_flow_style=False, sort_keys=False)
                logger.debug("Saved config file.")

    def validate(self, data, schema):
        """Invoke the schema-validator to normalize the config."""
        return _validate(data, schema)
    
    def get(self, *keys, default=None):
        """Get nested config value (an environment override wins: ENV_OVERRIDES)"""
        found, value = env_override(keys)
        if found:
            return value
        with self.lock:
            value = self.data
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return default
                if value is None:
                    return default
            return value
    
    def ensure_login_credentials(self):
        return ensure_login_credentials(self)
    
    
    def set(self, *keys, value):
        """Set nested config value"""
        with self.lock:
            # Navigate to parent
            data = self.data
            for key in keys[:-1]:
                if key not in data:
                    data[key] = {}
                data = data[key]
            # Set value
            data[keys[-1]] = value
    
    def get_all(self):
        """Get entire config as dict"""
        with self.lock:
            return self.data.copy()