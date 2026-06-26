# Booqable Parity Trailer Test Data

Fictitious company for workflow comparison: **Cape Trailworks Rentals & Service**.

## Company setup
- Company name: Cape Trailworks Rentals & Service
- Public/store name: Cape Trailworks
- Email: bookings@capetrailworks.test
- Phone: +27 21 555 0188
- Website: https://capetrailworks.test
- Country: South Africa
- Address: 18 Marine Drive, Paarden Eiland, Cape Town, Western Cape, 7405
- Timezone: Africa/Johannesburg
- Currency: ZAR / R
- Tax: VAT 15%, exclusive unless Booqable setup flow forces another default
- Default pickup: 08:00
- Default return: 16:00
- Rental period: dates and times, 60-minute increments
- Store copy: Trailer hire, trailer parts, and workshop service options for Cape Town businesses and weekend movers.

## Rental trailers
| Name | SKU | Type | Quantity | Price | Deposit | Public | Notes |
|---|---:|---|---:|---:|---:|---|---|
| 750kg Utility Trailer | CTW-TRL-750U | Rental product | 4 | R250/day | R1,000 | Yes | General-purpose open trailer, unbraked. |
| 1.5 Ton Braked Trailer | CTW-TRL-1500B | Rental product | 3 | R450/day | R1,500 | Yes | Braked axle, furniture and equipment moves. |
| Enclosed Furniture Trailer | CTW-TRL-ENC | Rental product | 2 | R650/day | R2,500 | Yes | Weather-protected box trailer. |
| Car Transporter Trailer | CTW-TRL-CAR | Rental product | 1 | R950/day | R4,000 | Yes | Vehicle transporter with ramps and winch. |
| Bike Trailer | CTW-TRL-BIKE | Rental product | 2 | R300/day | R1,200 | Yes | Two-bike motorbike trailer. |

## Parts for sale
| Name | SKU | Type | Quantity | Price | Public | Notes |
|---|---:|---|---:|---:|---|---|
| LED Trailer Light Kit | CTW-PART-LIGHTKIT | Sales item | 20 | R475 | Yes | Left/right LED light kit with wiring. |
| 48mm Jockey Wheel | CTW-PART-JOCKEY48 | Sales item | 15 | R695 | Yes | Clamp-on jockey wheel. |
| Coupler Lock | CTW-PART-LOCK | Sales item | 25 | R285 | Yes | Anti-theft hitch/coupler lock. |
| 7 Pin Trailer Plug | CTW-PART-7PIN | Sales item | 30 | R95 | Yes | Replacement 7-pin plug. |
| Wheel Bearing Kit | CTW-PART-BEARING | Sales item | 18 | R350 | Yes | Standard trailer wheel bearing kit. |

## Workshop/service options
| Name | SKU | Type | Quantity | Price | Public | Notes |
|---|---:|---|---:|---:|---|---|
| Trailer Safety Inspection | CTW-SVC-SAFE | Service | 999 | R550 | Yes | Lights, tyres, coupling, brakes, chassis check. |
| Bearing Replacement Labour | CTW-SVC-BEARING | Service | 999 | R750 | Yes | Labour only; parts billed separately. |
| Light Wiring Repair | CTW-SVC-WIRING | Service | 999 | R650 | Yes | Diagnose and repair trailer wiring faults. |
| Brake Service | CTW-SVC-BRAKES | Service | 999 | R1,250 | Yes | Brake adjustment/service on braked trailers. |
| Coupler Replacement Labour | CTW-SVC-COUPLER | Service | 999 | R600 | Yes | Labour only; parts billed separately. |

## Customers for test flow
| Customer | Type | Email | Phone | Marketing |
|---|---|---|---|---|
| Nomvula Dlamini | Individual | nomvula@sample.test | +27 82 555 0101 | Yes |
| Metro Events Logistics | Company | ops@metroevents.test | +27 21 555 0199 | No |

## Orders to run in both systems
1. Draft order for Nomvula Dlamini: 1 × 750kg Utility Trailer, pickup 2026-07-06 08:00, return 2026-07-08 16:00; then reserve, start, return.
2. Draft order for Metro Events Logistics: 1 × Enclosed Furniture Trailer + 2 × LED Trailer Light Kit + 1 × Trailer Safety Inspection, pickup 2026-07-10 08:00, return 2026-07-12 16:00; generate quote, contract, invoice, packing slip; record partial then full payment.
3. Overlap test: attempt to reserve 2 × Car Transporter Trailer for same dates when only 1 exists; expect shortage/prevention in both systems.

## Parity checklist pages
- Continue setup
- Settings: company, pricing, taxes, rental period, payments
- Inventory: rental product, sales item, service creation and filters
- Customers: individual/company creation, search, detail
- Orders: draft creation, product/service/sales lines, totals, deposits, status transitions
- Calendar: reservation visibility
- Documents: quote/contract/invoice/packing slip generation
- Online store: public catalogue, product pages, checkout/request flow
- Reports: order/revenue/product/customer summaries
