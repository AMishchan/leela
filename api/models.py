from django.db import models

# Create your models here.
import secrets
from django.db import models

class ApiKey(models.Model):
    name = models.CharField(max_length=100, unique=True)
    key = models.CharField(max_length=64, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    # опционально: список IP, с которых разрешено (через запятую)
    allowed_ips = models.CharField(max_length=500, blank=True, default="")

    @staticmethod
    def generate():
        return secrets.token_hex(32)  # 64 hex-символа