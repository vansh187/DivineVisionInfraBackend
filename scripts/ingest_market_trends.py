"""
One-off / periodic script: set the going ₹-per-sq-yd rate for a city so the
inventory layer can show estimated plot prices and honour budget filters
(divine_market_trends). Prices are NOT static - re-run this whenever the sales
team revises the rate; the newest row wins on every read.

Two modes:

  # hand-entered figure (the usual monthly/quarterly refresh)
  python scripts/ingest_market_trends.py --city Sonipat --property-type plot \
      --price-per-sqyd 33000 --previous-price-per-sqyd 31000 \
      --locality "Sector-15, Ganaur" --demand-score 68 --supply-score 55 \
      --rental-yield 2.6 --period "Indicative - Sep 2026"

  # derive it from real paid bookings once enough exist (total consideration / area)
  python scripts/ingest_market_trends.py --city Sonipat --property-type plot --from-bookings
"""
import sys
import argparse
from datetime import date

from _bootstrap import setup
setup()

from Divinepersistence import persistenceMarketTrend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True)
    parser.add_argument("--property-type", default="plot",
                        choices=["plot", "floor", "flat", "commercial"])
    parser.add_argument("--locality", default=None)
    parser.add_argument("--price-per-sqyd", type=float, default=None,
                        help="required unless --from-bookings")
    parser.add_argument("--from-bookings", action="store_true",
                        help="compute the rate from real booking-application documents instead")
    parser.add_argument("--previous-price-per-sqyd", type=float, default=None)
    parser.add_argument("--rental-yield", type=float, default=None)
    parser.add_argument("--demand-score", type=float, default=None)
    parser.add_argument("--supply-score", type=float, default=None)
    parser.add_argument("--sample-size", type=int, default=0)
    parser.add_argument("--period", default=None, help="period_label, e.g. 'Indicative - Sep 2026'")
    args = parser.parse_args()

    persistence = persistenceMarketTrend()

    price = args.price_per_sqyd
    sample = args.sample_size
    if args.from_bookings:
        rate, n = persistence.average_booking_rate_per_sqyd(city=args.city)
        if not rate:
            print(f"Not enough booking data to derive a rate ({n} usable booking(s)). "
                  f"Pass --price-per-sqyd instead.")
            sys.exit(1)
        price, sample = rate, n
        print(f"Derived {price:,.0f} /sq yd from {n} booking(s).")
    if price is None:
        parser.error("--price-per-sqyd is required unless --from-bookings is given")

    row = persistence.upsert_trend(
        city=args.city,
        property_type=args.property_type,
        price_per_sqyd=price,
        locality=args.locality,
        period_label=args.period,
        as_of_date=date.today(),
        previous_price_per_sqyd=args.previous_price_per_sqyd,
        rental_yield_percent=args.rental_yield,
        demand_score=args.demand_score,
        supply_score=args.supply_score,
        sample_size=sample,
    )
    print(f"Stored: {row.city} / {row.locality or '-'} / {row.property_type} "
          f"= {float(row.price_per_sqyd):,.0f} /sq yd  (as of {row.as_of_date}, "
          f"period '{row.period_label}', sample_size {row.sample_size})")


if __name__ == "__main__":
    main()
