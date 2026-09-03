# 🧭 Principio básico y decisión crítica

Tu app necesita acceder a Gmail periódicamente, **offline en nombre del usuario** → debes almacenar de forma persistente (y segura) al menos el **`refresh_token`** de Google por usuario.  
❌ **No guardes tokens en cookies/session.**  
En sesión guarda solo `user_id` o un identificador corto.

> ⚠️ **Nota importante:**  
> Google solo da `refresh_token` la primera vez que el usuario concede permisos, o si fuerzas `prompt=consent`.  
> Para producción, usa:
> - `access_type='offline'`
> - `prompt='consent'` la primera vez  
> - `include_granted_scopes='true'` cuando corresponda


---

# 🗄️ Qué guardar en la base de datos (y por qué)

Guarda estos campos por usuario.  
Los marcados con **(ENCRIPTAR)** deben guardarse cifrados en la BD o en una columna con cifrado.

## Tabla `users`

| Campo | Tipo | Descripción |
|--------|------|-------------|
| id | UUID (PK) | Identificador único |
| email | string, único | Dirección Google |
| name | string | Nombre del usuario |
| created_at, updated_at | timestamp | Fechas de creación y actualización |
| is_active, is_admin | boolean | Estado y rol del usuario |

## Tabla `oauth_tokens`

| Campo | Tipo | Descripción |
|--------|------|-------------|
| id | UUID (PK) | Identificador único |
| user_id | FK → users.id | Relación con usuario |
| provider | string | e.g. `"google"` |
| client_id | string | Opcional |
| scopes | text/array | Scopes concedidos |
| access_token | text | Opcional; se puede regenerar |
| access_token_expires_at | timestamp | Expiración del access token |
| refresh_token | text (ENCRIPTAR) | Obligatorio para acceso offline |
| token_response_raw | json (ENCRIPTAR opcional) | Guarda el JSON completo |
| last_refreshed_at | timestamp | Última actualización |
| revoked | boolean | Revocado o no |
| created_at, updated_at | timestamp | Control temporal |

## Tabla `processed_emails`

| Campo | Tipo | Descripción |
|--------|------|-------------|
| id | UUID | Identificador |
| user_id | FK | Usuario |
| gmail_message_id | string | ID de Gmail (para evitar reprocesar) |
| from_address, subject, date_received | string / date | Datos del mensaje |
| is_newsletter | boolean | Detección de boletines |
| language | string | Idioma detectado |
| summary_path | string | Ruta S3 o local del resumen |
| summary_type | enum | `"short"`, `"detailed"`, `"schema"` |
| image_path | string | Imagen generada |
| sent_email_id | string | ID del correo reenviado |
| status | enum | `"queued"`, `"processing"`, `"done"`, `"error"` |
| error_msg | text | Mensaje de error |
| created_at, processed_at | timestamp | Tiempos de creación/procesado |

## Tabla `jobs`

| Campo | Descripción |
|--------|-------------|
| id, user_id | Identificadores |
| scheduled_for, started_at, finished_at | Control temporal |
| status | Estado |
| details (json) | Detalles del job |

---

# 🔐 Esquema de seguridad (no negocies esto)

- **Encriptación en reposo:**  
  Cifrar `refresh_token` con **KMS (AWS KMS recomendado)** o con una key gestionada por Vault.  
  Si no tienes KMS, usa **Fernet** con key almacenada en **Secrets Manager** (no en `.env`).

- **Minimiza scopes:**  
  Usa solo lo necesario:  
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/gmail.send` (si envías correos)
  - Añade `userinfo.email` y `userinfo.profile` si quieres info del usuario.

- **Rotación de claves y revocación:**  
  - Campo `revoked`.  
  - Maneja errores `invalid_grant` con re-autenticación del usuario.

- **Obligatorio en producción:**  
  - HTTPS  
  - Cookies seguras (`SESSION_COOKIE_SECURE=True`)

- **No guardes `client_secret` en el repo.**  
  Usa **AWS Secrets Manager / Parameter Store**.

- **Auditoría:**  
  Logs de uso y accesos a tokens.

---

# 🔄 Flujo OAuth recomendado (primer consent + persistencia)

```python
authorization_url, state = flow.authorization_url(
    access_type='offline',
    include_granted_scopes='true',
    prompt='consent'
)
```

Tras `flow.fetch_token(code=code)`:
- Guarda `refresh_token` cifrado y `scopes`.
- En `session` guarda solo `user_id`.

> Si ya tenías token y `refresh_token` ausente (reconsent del usuario), pide `prompt='consent'` para forzar su emisión.

---

# 🧠 Cómo refrescar token y usar Gmail en background

```python
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

creds = Credentials(
    token=None,
    refresh_token=refresh_token_plain,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    scopes=scopes
)
creds.refresh(Request())
# Ahora creds.token y creds.expiry están listos
```

> Si `refresh()` falla con `invalid_grant`, marca `revoked=True` y notifica al usuario.

---

# ⚙️ Pipeline detallado (cada job cada 2 días)

1. **Scheduler:**  
   (Celery beat / AWS EventBridge / Redis) lanza `process_mail_for_user(user_id)`

2. **Worker:**
   - Carga y descifra `refresh_token`
   - Refresca credenciales
   - Llama a `Gmail API` con filtros
   - Procesa mensajes nuevos:
     - Detecta newsletter
     - Traduce si es inglés
     - Resume texto
     - Genera imagen
     - Envía correo
     - Guarda JSON / MD estructurado

3. **Logging, retries y error handling.**

---

# 🧾 Detección de newsletters (heurísticas)

- `List-Unsubscribe` header → casi seguro newsletter  
- `From:` con dominios tipo newsletter/noreply  
- `Precedence: bulk`  
- Cuerpo con muchos links o iconos sociales  
- Clasificador ML opcional para reducir falsos positivos

---

# 🤖 Recomendaciones de modelos (local y API)

### Traducción (EN → ES)
- **Local:** `Helsinki-NLP/opus-mt-en-es`
- **Alta calidad:** `facebook/m2m100_1.2B`
- **API:** OpenAI / DeepL

### Resumen
- **Local clásico:** `sshleifer/distilbart-cnn-12-6`
- **Instructivo:** `flan-t5-large`
- **API:** GPT (OpenAI, Anthropic)

### Formateo
- **Local:** flan-t5 o t5 con prompt “hazlo atractivo”
- **API:** GPT instruct-tuned

### Imagen
- **Local:** Stable Diffusion (v1.5 o SDXL)
- **API:** Midjourney / DALL·E / Stability.ai

---

# ☁️ Infraestructura propuesta (AWS-friendly)

| Componente | Recomendado |
|-------------|--------------|
| Compute | Flask + Celery (ECS Fargate / EKS) |
| Scheduler | Celery Beat / AWS EventBridge |
| DB | Postgres (RDS) |
| Cache | Redis (ElastiCache) |
| Storage | S3 |
| Secrets | AWS Secrets Manager + KMS |
| Monitoring | CloudWatch / Prometheus + Grafana |

---

# 🧩 Ejemplos SQL y snippets

### Tabla `oauth_tokens` (Postgres)

```sql
CREATE TABLE oauth_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES users(id),
  provider text NOT NULL DEFAULT 'google',
  client_id text,
  scopes text[],
  access_token text,
  access_token_expires_at timestamptz,
  refresh_token text, -- encrypted
  token_response_json jsonb,
  last_refreshed_at timestamptz,
  revoked boolean DEFAULT false,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
```

### Guardar token (Python pseudocode)

```python
from cryptography.fernet import Fernet

def encrypt(plain: str) -> str:
    f = Fernet(ENCRYPTION_KEY)
    return f.encrypt(plain.encode()).decode()

def decrypt(token_enc: str) -> str:
    f = Fernet(ENCRYPTION_KEY)
    return f.decrypt(token_enc.encode()).decode()
```

### Refrescar token

```python
creds.refresh(Request())
# Guarda creds.token y creds.expiry en la BD (opcional)
```

---

# 🛠️ Diseño del worker (pseudocódigo)

```python
def process_user_mail(user_id):
    token = load_token(user_id)
    creds = refresh_credentials(token)
    gmail = build('gmail','v1',credentials=creds)
    messages = gmail.users().messages().list(userId='me', q="from:example@newsletter.com", maxResults=50).execute()
    for m in messages['messages']:
        if already_processed(m['id']): continue
        message = gmail.users().messages().get(userId='me', id=m['id'], format='full').execute()
        text = extract_text_from_message(message)
        is_newsletter = detect_newsletter(message, text)
        if not is_newsletter:
            mark_processed_skip()
            continue
        lang = detect_language(text)
        if lang == 'en':
            text_es = translate(text)
        else:
            text_es = text
        summary = summarize(text_es)
        formatted = format_email(summary, metadata)
        image_path = generate_image(summary or title)
        sent_id = send_email_via_gmail(gmail, to=user_email, subject=..., body=formatted, image=image_path)
        save_structured_json(...)
```

---

# 🚀 Elección práctica para empezar (MVP)

### MVP local
- Flask + SQLite + Celery (redis)
- Guardar `refresh_token` cifrado
- Modelos:
  - Traducción: `opus-mt-en-es`
  - Resumen: `flan-t5-small`
  - Imagen: Stable Diffusion / Stability API
- Scheduler: Celery beat cada 48h

### Producción
- Postgres (RDS)
- S3, Redis (ElastiCache)
- ECS/EKS workers
- Secrets Manager + KMS
- HTTPS + ALB

---

# ⚡ Puntos críticos (errores comunes)

- No usar `prompt=consent` → sin `refresh_token`
- Guardar `refresh_token` sin cifrar → **riesgo grave**
- Persistir en sesión → se pierde al reiniciar
- No manejar `invalid_grant`
- Ignorar rate limits → bloqueos

---

# 🧩 Próximos pasos concretos

1. Implementar tablas `users` + `oauth_tokens`  
2. Crear función segura `save_refresh_token()` con KMS/Fernet  
3. Guardar `refresh_token` en DB en el callback OAuth  
4. Worker que refresca token y lista mensajes  
5. Pipeline: extract → detect newsletter → translate → summarize → save JSON  
6. Añadir envío de email con Gmail API  
7. Añadir generación de imagen  
8. Orquestar scheduler (Celery beat / EventBridge)

---

📘 **Fin del documento**
