"""Choose a vendor renewal offer from captured rates."""
from .rate_cards import rates


def cheapest(cards, product):
    offers = [card for card in rates(cards) if card['product'] == product]
    return min(offers, key=lambda card: card['annual_price']) if offers else None
