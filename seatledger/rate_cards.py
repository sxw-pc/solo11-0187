"""Capture offline supplier renewal rates."""

def rates(cards):
    return [card for card in cards if card.get('product') and card.get('annual_price') is not None]
