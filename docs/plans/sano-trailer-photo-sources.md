# Sano Trailers — Default Trailer Product Photo Sources

Harvested from the Sano Trailers website (https://sanotrailers.co.za) on 2026-09-23.
Images were downloaded at full resolution from the GoDaddy `img1.wsimg.com` CDN and saved to:

`static/img/trailer-categories/`

**Method:** Browser tools failed to start (`chrome-not-running` daemon error), so I fell back to `curl` on the raw site HTML, extracted image URLs from `src`/`data-srclazy`/`srcset`/background-image attributes, then `curl`ed each CDN image at its full-resolution base URL (stripping the `/:/rs=w:...` resize suffix). Every file was verified with PIL (`Image.open().verify()`) and deduplicated by MD5.

The site is a GoDaddy Website Builder site with 5 pages: `/`, `/trailer-hire`, `/trailer-sales`, `/trailer-repairs`, `/contact-us`. The `/trailer-hire` page is a "menu" where each trailer category's photo `<img>` immediately precedes its category heading in the source — that ordering was used to map photo → category (confirmed via the `data-aid="MENU_SECTION0_ITEMn_IMAGE"` markers).

---

## Images saved (15 total)

Base CDN URL: `https://img1.wsimg.com/isteam/ip/a3db7348-e1ab-4bd3-ba9c-ce353caf1da8/<file>`

### Trailer category photos (source page: https://sanotrailers.co.za/trailer-hire)

| Local filename | Original image URL (relative to base) | Dimensions | Guessed trailer type / category |
|---|---|---|---|
| `single-axle-trailer.png` | `20260524_182207903_iOS.png` | 1512×1512 | Single-axle red cage/box utility trailer (studio shot) |
| `double-axle-trailer.png` | `20260524_184658283_iOS.png` | 1512×1512 | Double/tandem-axle red mesh-cage box trailer (studio shot) |
| `luggage-trailer.png` | `blob-72db1ad.png` | 1512×1512 | Single-axle enclosed Venter-style luggage trailer (white, studio) |
| `closed-box-trailer.png` | `20260524_185427095_iOS.png` | 1512×1512 | Single-axle enclosed box / luggage trailer (white, studio) |
| `single-axle-tarp-trailer.png` | `blob-7ce526c.png` | 1512×1512 | Single-axle covered box trailer with soft-top tarp/canvas |
| `double-axle-car-trailer.png` | `20260524_193507654_iOS.png` | 1512×1512 | Tandem-axle open flatbed car-transporter trailer (no tarp) |
| `single-axle-car-trailer.png` | `20260524_191703509_iOS.png` | 1512×1512 | Single-axle flatbed trailer with loading ramps (car trailer) |
| `piggyback-dolly-trailer.png` | `20260524_191029754_iOS.png` | 1512×1512 | Small flatbed utility trailer with stowed ramps (piggyback/dolly category) |
| `quad-bike-golf-cart-trailer.png` | `20260524_193936051_iOS.png` | 1512×1512 | Single-axle open utility/landscape trailer (quad bike / golf cart) |
| `livestock-trailer.png` | `blob-af9b724.png` | 1512×1512 | Tall single-axle cage trailer with arched canvas supports (livestock) |
| `single-axle-flatbed-trailer.png` | `20260524_191200731_iOS.png` | 1512×1512 | Open flatbed trailer with front headboard rack |
| `double-axle-flatbed-trailer.png` | `20260524_193612957_iOS.png` | 1512×1512 | Tandem-axle open flatbed trailer with front headboard rack |

### Extra / alternate photos (source page: https://sanotrailers.co.za/)

| Local filename | Original image URL (relative to base) | Dimensions | Guessed trailer type / category |
|---|---|---|---|
| `luggage-trailer-rental-glider.jpg` | `fb_505715248230883_1170x881.jpg` | 1170×881 | "Glider" enclosed luggage trailer branded SANO TRAILERS rental, towed by van |
| `tow-dolly-piggyback-trailer.jpg` | `blob-0009.png` (JPEG content) | 2560×1920 | Red tow dolly / piggyback car dolly on roadside |
| `cage-trailer-burquip-yard.jpg` | `blob-0012.png` (JPEG content) | 2560×1920 | Red single-axle mesh cage trailer in a yard — **third-party "Burquip" branding visible** |

---

## Categories / trailer types with NO usable photo

- **Mobile kitchen trailers** — listed on `/trailer-sales` ("we supply ... mobile kitchen trailers"), no photo anywhere on the site.
- **Bobcat trailers** — listed on `/trailer-sales` ("... bobcat trailers"), no photo anywhere on the site.

### Notes on other non-product images (not saved)

- `/` homepage "Trailer Hire.png", "Trailer Sales.png", "Trailer Repairs.png" — decorative wide banner graphics (1371×520) with landscape backgrounds, not trailer product photos.
- `/trailer-repairs` page has 3 service photos (`Trailer Repairs.jpg`, `Trailer Maintenance.jpg`, `Trailer Refurbishment.jpg`) showing repair/maintenance work, not trailer product categories.
- Site logo and favicons were skipped.
- The `/trailer-sales` and `/contact-us` pages contain no trailer product photos.
- Every category on the `/trailer-hire` rental price list (12 categories: single axle, double axle, luggage, closed box, single axle w/ tarp, double axle car, single axle car, piggyback/dolly, quad bike/golf cart, single axle livestock, single axle flatbed, double axle flatbed) **does** have a photo — saved above.

## Caveats

- `cage-trailer-burquip-yard.jpg` shows a competitor's ("Burquip") signage/branding and is likely unsuitable as a default Sano/ABI category photo — kept for reference only.
- Studio photos are 1512×1512 PNGs (0.4–1.0 MB each); the three homepage JPEGs are large (up to 1.9 MB) and may need downscaling before use as thumbnails.
