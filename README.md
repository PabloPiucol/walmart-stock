# Bsale → Walmart Chile Stock

Servicio Docker para preparar y enviar manualmente el stock disponible de una sucursal Bsale a Walmart Chile mediante feeds masivos relacionados por SKU.

## Inicio

1. Copia `.env.example` a `.env` y completa:
   - `BSALE_ACCESS_TOKEN`
   - `WALMART_CLIENT_ID`
   - `WALMART_CLIENT_SECRET`
   - `WALMART_PARTNER_ID`
   - `WALMART_CHANNEL_TYPE`, si Walmart te lo asignó
2. Ejecuta:

```bash
docker compose up --build -d
```

3. Abre `http://TU_HOST:3011/settings`, selecciona la sucursal Bsale y vuelve a la página principal.

La UI no incluye autenticación. Expón el puerto solamente dentro de una red privada.
Ejecuta una sola réplica del servicio: SQLite y el bloqueo de sincronización están diseñados para un único proceso.

## Autenticación Walmart

El servicio implementa el flujo oficial `client_credentials`:

1. Solicita un access token mediante [`POST /v3/token`](https://developer.walmart.com/cl-marketplace/reference/tokenapi), usando Basic Auth y `WM_PARTNER.ID`.
2. Valida el token mediante [`GET /v3/token/detail`](https://developer.walmart.com/cl-marketplace/reference/gettokendetail) antes de cada envío o reanudación.
3. Si una operación de Walmart responde `401`, renueva y valida el token una sola vez antes de reintentar.

En **Configuración → Walmart Chile** puedes probar este flujo manualmente. La última fecha, estado y respuesta resumida quedan visibles sin persistir tokens ni secretos.

## Funcionamiento

- **Generar vista previa** consulta solamente Bsale y no modifica stock.
- Durante la preparación, la página muestra la etapa, porcentaje y contador.
- Los productos se relacionan por SKU exacto, ignorando espacios exteriores.
- Los SKU faltantes o duplicados en Bsale se reportan como errores locales y no se envían.
- Los productos exclusivos de Walmart quedan intactos.
- **Enviar productos** divide automáticamente las cantidades guardadas en feeds masivos menores a 5 MB.
- Antes de enviar o reanudar, el servicio obtiene un token con `client_credentials` y lo valida mediante `GET /v3/token/detail`.
- Walmart aplica los SKU existentes y los SKU rechazados, incluidos los inexistentes, se registran como omitidos.
- El servicio procesa los feeds secuencialmente, espera cada resultado durante un máximo configurable de 35 minutos y conserva todos sus `feedId`.
- Si Walmart todavía no publica el estado de un feed, el servicio continúa consultándolo hasta el timeout.
- Las ejecuciones fallidas con feeds enviados pueden reanudar su seguimiento sin reenviarlos.
- Los valores negativos de Bsale se convierten en cero.
- Una vista previa puede aplicarse una sola vez.
- El historial se conserva durante 90 días.

## Variables

| Variable | Predeterminado | Descripción |
| --- | --- | --- |
| `BSALE_API_URL` | `https://api.bsale.io/v1` | API Bsale |
| `WALMART_CLIENT_ID` | obligatorio | Client ID usado en Basic Auth |
| `WALMART_CLIENT_SECRET` | obligatorio | Client secret usado en Basic Auth |
| `WALMART_API_URL` | `https://marketplace.walmartapis.com` | API Walmart Marketplace |
| `WALMART_MARKET` | `cl` | Mercado Walmart |
| `WALMART_PARTNER_ID` | obligatorio | Partner ID enviado como `WM_PARTNER.ID` |
| `WALMART_CHANNEL_TYPE` | vacío | Channel type, si Walmart lo asignó |
| `WALMART_FEED_TIMEOUT_SECONDS` | `2100` | Tiempo máximo de seguimiento del feed |
| `WALMART_FEED_POLL_SECONDS` | `30` | Intervalo entre consultas de estado |
| `DATABASE_URL` | `sqlite:////data/app.db` | Base de datos del historial |

El feed JSON usa el contrato oficial de Walmart Chile `InventoryHeader` versión `1.4` y cantidades en unidades `EACH`.

## Desarrollo

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --reload --port 3011
```

Endpoints operativos:

- `GET /health`
- `GET /`
- `GET /settings`
- `POST /settings/office`
- `POST /settings/walmart/test`
- `POST /sync/preview`
- `GET /runs/{id}`
- `GET /runs/{id}/status`
- `POST /runs/{id}/cancel`
- `POST /runs/{id}/apply`
- `POST /runs/{id}/resume`
