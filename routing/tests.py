from decimal import Decimal

from django.test import SimpleTestCase

from routing.management.commands.load_stations import clean_stations


def _row(opis_id, state='TX', price='3.50'):
    return {
        'opis_id': opis_id,
        'name': f'Station {opis_id}',
        'address': 'I-10, EXIT 1',
        'city': 'Austin',
        'state': state,
        'price': Decimal(price),
    }


class CleanStationsTests(SimpleTestCase):
    def test_drops_canadian_rows(self):
        cleaned = clean_stations([_row(1, 'ON'), _row(2, 'BC'), _row(3, 'TX')])
        self.assertEqual([s['opis_id'] for s in cleaned], [3])

    def test_dedupes_keeping_lowest_price(self):
        cleaned = clean_stations([_row(1, price='3.90'), _row(1, price='3.10'), _row(1, price='3.50')])
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]['price'], Decimal('3.10'))
