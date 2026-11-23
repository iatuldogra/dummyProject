# E-commerce microservices (FastAPI + PostgreSQL + Kubernetes)

Six small FastAPI services, each owning its own PostgreSQL database, talking to each other over synchronous HTTP (`httpx`).

## Architecture

```
                         ┌───────────────────────── Ingress (nginx) ─────────────────────────┐
                         │ /users  /products  /orders  /payments  /promotions  /support      │
                         └───┬────────┬─────────┬─────────┬───────────┬────────────┬─────────┘
                             │        │         │         │           │            │
                       users-svc products-svc orders-svc payments-svc promotions-svc support-svc
                             │        ▲  ▲      │  │  │     ▲  ▲          ▲            │  │
                             │        │  └──────┘  │  └─────┘  │          │            │  │
                             │        │  stock     │  charge   │          │            │  │
                             │        │            └───────────┼──────────┘ promo/     │  │
                             │        │                        │          campaigns    │  │
                             │        │                        └── refunds ────────────┘  │
                             │        │                     orders-svc ◄── verify order ──┘
                             ▼        ▼          ▼         ▼           ▼            ▼
                         users_db products_db orders_db payments_db promotions_db support_db
                                         (one Postgres instance, six databases)
```

| Service | Path prefix | Database | Responsibilities |
|---|---|---|---|
| users | `/users` | `users_db` | Registration, login, JWT issuing, `/users/me` |
| products | `/products` | `products_db` | Catalog CRUD, inventory (set / atomic adjust) |
| orders | `/orders` | `orders_db` | Cart, checkout (prices from products, campaign + promo discounts from promotions, stock reservation, payment), stores `subtotal`, `discount`, `total` |
| payments | `/payments` | `payments_db` | Mock charges, `POST /payments/refunds` |
| promotions | `/promotions` | `promotions_db` | Promo codes CRUD + `POST /promotions/promo-codes/validate`; campaigns (sales events) CRUD + `GET /promotions/campaigns/active` |
| support | `/support` | `support_db` | Complaints (tickets) and returns (verified via orders, refunded via payments) |

Every service serves its routes under its own path prefix (so the Ingress can route by prefix with no rewrites, and the same URLs work locally, in-cluster and through the Ingress) and exposes `GET /health` at the root. Interactive docs are at `/docs` on each service.

### Auth model

- `users-service` issues HS256 JWTs (`sub`, `email`, `role`). All services share `JWT_SECRET` and validate tokens locally, so a token can't be revoked individually: after changing `ADMIN_PASSWORD` (or any suspected leak) also rotate `JWT_SECRET` to invalidate every issued token.
- Login is throttled per email: after `LOGIN_MAX_FAILURES` failed attempts within `LOGIN_LOCKOUT_SECONDS` it returns `429` (counted per users-service replica).
- Registration always creates `customer` accounts. On startup users-service creates (or takes over) the bootstrap admin from `ADMIN_EMAIL` / `ADMIN_PASSWORD` (`admin@example.com` / `admin123` in Compose, whose ports bind to localhost only; password from `k8s/secrets.env` in k8s). Admins manage products, promo codes, campaigns, complaint status and return decisions.
- Service-only endpoints (inventory adjust/reserve/release, charge, void, refund, promo redeem/release) require the `X-Internal-Token` header (`INTERNAL_API_KEY`), so they can't be called by end users through the Ingress.

### Checkout flow (`POST /orders/checkout`)

1. Load the cart (or explicit `items`) and fetch each product from **products-service** (price, category, stock).
2. Fetch active campaigns from **promotions-service**; apply the best matching campaign per line (scope `catalog`, `category`, or `products`; `percent` or `fixed` per unit).
3. If a `promo_code` is given, validate it with **promotions-service** against the post-campaign subtotal (active flag, expiry, usage limit, minimum amount; `percent` or `fixed`).
4. Persist the order (`pending`); for cart checkouts this happens in the same transaction that removes the cart items.
5. Reserve stock in **products-service**, redeem the promo code in **promotions-service**, then charge **payments-service** (mock: card numbers ending in `0000` are declined → order `failed`). Each call carries an idempotency key derived from the order.
6. Mark the order `paid`. On failure the order becomes `failed`, the cart is restored, and stock, promo use and any charge are released/voided by the same keys, so calls whose response was lost are undone too. Compensations that can't complete (and checkouts left `pending` for `STALE_PENDING_MINUTES`) are retried by a background loop every `RECONCILE_INTERVAL` seconds.

### Returns flow

`POST /support/returns` verifies the order with **orders-service** (forwarding the user's token, so users can only return their own paid orders) and computes the refund proportionally to what was actually paid. When an admin sets the status to `approved`, support calls **payments-service** `POST /payments/refunds`; on success the return becomes `refunded`.

## Layout

```
ecommerce/
├── docker-compose.yml
├── db/init.sql                 # creates the 6 databases (local dev)
├── k8s/                        # kustomize-ready manifests
│   ├── kustomization.yaml
│   ├── namespace.yaml, secrets.env.example, configmap.yaml
│   ├── postgres.yaml           # StatefulSet + Service + init-script ConfigMap
│   ├── services/<name>.yaml    # Deployment (2 replicas) + ClusterIP Service
│   └── ingress.yaml
└── services/<name>/
    ├── Dockerfile, requirements.txt
    └── app/{main,models,schemas,database,auth}.py
```

## Run locally with Docker Compose

```bash
cd ecommerce
docker compose up --build -d
```

| Service | URL |
|---|---|
| users | http://localhost:8001 |
| products | http://localhost:8002 |
| orders | http://localhost:8003 |
| payments | http://localhost:8004 |
| promotions | http://localhost:8005 |
| support | http://localhost:8006 |

Tables are created automatically on startup. Use `docker compose down -v` to wipe data.

## Build images

```bash
cd ecommerce
for s in users products orders payments promotions support; do
  docker build -t ecommerce/$s-service:latest services/$s
done
```

(`docker compose build` produces the same image names.)

## Deploy to Kubernetes

The manifests reference `ecommerce/<name>-service:latest` with `imagePullPolicy: IfNotPresent`, so images loaded into the local cluster are used directly. For a real registry, push the images and set `images:` in `k8s/kustomization.yaml`. Secrets are not committed: create `k8s/secrets.env` from `k8s/secrets.env.example` with your own values before `kubectl apply -k` (kustomize fails without it).

The Ingress only serves HTTPS (HTTP is redirected). Install a certificate as the `ecommerce-tls` Secret (`kubectl -n ecommerce create secret tls ecommerce-tls --cert=tls.crt --key=tls.key`); until then ingress-nginx uses its self-signed default certificate and the `curl` examples need `-k`.

### minikube

```bash
minikube start
minikube addons enable ingress
cp ecommerce/k8s/secrets.env.example ecommerce/k8s/secrets.env   # then fill in every value
for s in users products orders payments promotions support; do
  minikube image load ecommerce/$s-service:latest
done
kubectl apply -k ecommerce/k8s
kubectl -n ecommerce get pods -w
export BASE=https://$(minikube ip)
```

### kind

```bash
cat <<'KIND' | kind create cluster --name ecommerce --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    labels:
      ingress-ready: "true"
    extraPortMappings:
      - containerPort: 80
        hostPort: 8080
      - containerPort: 443
        hostPort: 8443
KIND
cp ecommerce/k8s/secrets.env.example ecommerce/k8s/secrets.env   # then fill in every value
kubectl apply -f https://kind.sigs.k8s.io/examples/ingress/deploy-ingress-nginx.yaml
kubectl -n ingress-nginx wait --for=condition=ready pod -l app.kubernetes.io/component=controller --timeout=180s
for s in users products orders payments promotions support; do
  kind load docker-image ecommerce/$s-service:latest --name ecommerce
done
kubectl apply -k ecommerce/k8s
kubectl -n ecommerce wait --for=condition=available deploy --all --timeout=300s
export BASE=https://localhost:8443
```

## Example API calls

Set one base URL per service. Through the Ingress they are all the same host:

```bash
# Kubernetes (Ingress)
USERS=$BASE PRODUCTS=$BASE ORDERS=$BASE PAYMENTS=$BASE PROMOTIONS=$BASE SUPPORT=$BASE
# Docker Compose
USERS=http://localhost:8001 PRODUCTS=http://localhost:8002 ORDERS=http://localhost:8003
PAYMENTS=http://localhost:8004 PROMOTIONS=http://localhost:8005 SUPPORT=http://localhost:8006
```

The examples use `jq` to capture tokens and IDs.

### Register / login

```bash
curl -s -X POST $USERS/users/register -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"alice123","full_name":"Alice"}'

# Bootstrap admin (ADMIN_EMAIL / ADMIN_PASSWORD). On k8s use the ADMIN_PASSWORD from k8s/secrets.env.
ADMIN_PASSWORD=${ADMIN_PASSWORD:-admin123}
ADMIN=$(curl -s -X POST $USERS/users/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"admin@example.com\",\"password\":\"$ADMIN_PASSWORD\"}" | jq -r .access_token)
ALICE=$(curl -s -X POST $USERS/users/login -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"alice123"}' | jq -r .access_token)

curl -s $USERS/users/me -H "Authorization: Bearer $ALICE"
```

### Create a product

```bash
PRODUCT_ID=$(curl -s -X POST $PRODUCTS/products -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Noise-cancelling Headphones","category":"electronics","price":200.00,"stock":50}' | jq -r .id)

curl -s $PRODUCTS/products?category=electronics
curl -s $PRODUCTS/products/$PRODUCT_ID/inventory
```

### Create a Black Friday campaign

```bash
curl -s -X POST $PROMOTIONS/promotions/campaigns -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"Black Friday\",
        \"description\": \"20% off all electronics\",
        \"start_at\": \"$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)\",
        \"end_at\": \"$(date -u -d '+3 days' +%Y-%m-%dT%H:%M:%SZ)\",
        \"discount_type\": \"percent\",
        \"discount_value\": 20,
        \"scope\": \"category\",
        \"categories\": [\"electronics\"]
      }"

curl -s $PROMOTIONS/promotions/campaigns/active
```

`scope` can be `catalog` (everything), `category` (`categories: [...]`) or `products` (`product_ids: [...]`).

### Create a promo code

```bash
curl -s -X POST $PROMOTIONS/promotions/promo-codes -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"code":"SAVE10","discount_type":"fixed","discount_value":10,"usage_limit":100,
       "expires_at":"2030-12-31T23:59:59Z"}'

# Anyone can check a code against a subtotal
curl -s -X POST $PROMOTIONS/promotions/promo-codes/validate -H 'Content-Type: application/json' \
  -d '{"code":"SAVE10","subtotal":320}'
```

### Checkout with a promo code

```bash
curl -s -X POST $ORDERS/orders/cart/items -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' -d "{\"product_id\": $PRODUCT_ID, \"quantity\": 2}"

ORDER=$(curl -s -X POST $ORDERS/orders/checkout -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' \
  -d '{"promo_code":"SAVE10","card_number":"4242424242424242"}')
echo "$ORDER" | jq '{id, status, subtotal, discount, total, payment_id}'
# => subtotal 400.00, discount 90.00 (80 Black Friday + 10 SAVE10), total 310.00, status "paid"
ORDER_ID=$(echo "$ORDER" | jq -r .id)

curl -s $PAYMENTS/payments?order_id=$ORDER_ID -H "Authorization: Bearer $ALICE"
```

Use a card ending in `0000` to simulate a decline (HTTP 402, order `failed`, stock and promo released, cart restored).

### File a complaint

```bash
curl -s -X POST $SUPPORT/support/complaints -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' \
  -d "{\"order_id\": $ORDER_ID, \"subject\": \"Late delivery\", \"message\": \"My order has not arrived yet.\"}"

# Admin moves it along: open -> in_progress -> resolved
curl -s -X PATCH $SUPPORT/support/complaints/1 -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' -d '{"status":"in_progress"}'
```

### Request a return → refund

```bash
RETURN_ID=$(curl -s -X POST $SUPPORT/support/returns -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' \
  -d "{\"order_id\": $ORDER_ID, \"items\": [{\"product_id\": $PRODUCT_ID, \"quantity\": 1}], \"reason\": \"Defective left earcup\"}" \
  | jq -r .id)
# status "requested", refund_amount 155.00 (half of what was paid)

# Admin approves -> support calls payments-service POST /payments/refunds
curl -s -X PATCH $SUPPORT/support/returns/$RETURN_ID -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' -d '{"status":"approved"}' | jq '{status, refund_amount, refund_id}'
# => status "refunded"

curl -s $PAYMENTS/payments?order_id=$ORDER_ID -H "Authorization: Bearer $ALICE" | jq '.[0] | {status, refunded_amount}'
# => status "partially_refunded", refunded_amount 155.00
```

## Configuration

| Variable | Used by | Description |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | all | Postgres connection (or set `DATABASE_URL`) |
| `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_EXPIRE_MINUTES` | all / users | JWT signing & validation |
| `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_SECONDS` | users | Failed-login throttling (default 5 per 900 s) |
| `INTERNAL_API_KEY` | all | Shared key for service-to-service endpoints |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | users | Bootstrap admin account seeded at startup |
| `PRODUCTS_SERVICE_URL`, `PROMOTIONS_SERVICE_URL`, `PAYMENTS_SERVICE_URL` | orders | Downstream URLs |
| `ORDERS_SERVICE_URL`, `PAYMENTS_SERVICE_URL` | support | Downstream URLs |
| `HTTP_TIMEOUT` | orders, support | Downstream call timeout in seconds (default 5) |
| `RECONCILE_INTERVAL`, `STALE_PENDING_MINUTES` | orders | Failed-checkout compensation retry interval (default 30 s) and age after which a `pending` order counts as interrupted (default 15) |

## Limitations (by design, to keep it minimal)

- Payments are mocked; no external gateway. No message queue: all calls are synchronous, and checkout uses compensating actions (stock release) instead of distributed transactions.
- Tables are created with `metadata.create_all` at startup; columns added to existing tables are applied by idempotent `ALTER TABLE ... IF NOT EXISTS` statements (`MIGRATIONS` in each service's `database.py`). Use Alembic for anything more involved.
- Single Postgres instance for all six databases.
