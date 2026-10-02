from django.db import models


class FuelStation(models.Model):
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=10, decimal_places=8)
    lat = models.FloatField()
    lng = models.FloatField()

    class Meta:
        ordering = ['opis_id']

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'
