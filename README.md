# NewsPulse

NewsPulse es una aplicación personal que conecta una cuenta de Gmail,
detecta newsletters nuevas y conserva su contenido legible para las
siguientes etapas de generación del correo maestro.

## Estado del proyecto

P0 estabilizó la autenticación, la configuración y el acceso seguro a
Gmail. P1 dispone ya de ingestión incremental, clasificación y
extracción de contenido.

Actualmente están implementados:

- Aplicación web Flask.
- Autenticación OAuth 2.0 con Google.
- Validación de `state` frente a CSRF.
- Persistencia cifrada de credenciales OAuth con Fernet y SQLite.
- Renovación automática de credenciales caducadas.
- Sesión firmada con un identificador monousuario.
- Consulta del perfil y listado básico de mensajes Gmail.
- Ingestión incremental desde el comienzo de uso de la aplicación.
- Clasificación explicable de newsletters mediante metadatos.
- Reglas configurables por remitente, dominio, `List-Id` y etiqueta.
- Gestión web de fuentes incluidas y excluidas.
- Descarga segura y recursiva del contenido MIME.
- Extracción de texto legible y enlaces HTTP o HTTPS.
- Persistencia local del contenido con estados y reintentos.
- Purga automática del contenido al finalizar su retención.
- Logout mediante `POST`.
- Pruebas unitarias, de integración y de rutas Flask.

Todavía no están implementados:

- Traducción y resumen mediante IA.
- Generación de imágenes.
- Construcción y envío del correo maestro.
- Programación periódica de la ejecución.
- Despliegue de producción.

## Requisitos

- Python 3.12.
- Proyecto de Google Cloud.
- Gmail API habilitada.
- Cliente OAuth de tipo aplicación web.
- Cuenta de Google autorizada como usuario de prueba mientras la
  aplicación OAuth esté en modo de pruebas.

## Estructura principal

```text
app/config.py
    Configuración centralizada y validación del entorno.

app/backend/web_app.py
    Aplicación Flask, rutas OAuth, sesión y endpoints.

app/backend/bd/db.py
    Inicialización y esquema SQLite.

app/backend/bd/oauth_store.py
    Cifrado y persistencia de credenciales OAuth.

app/backend/bd/ingestion_store.py
    Cursor incremental, mensajes, clasificaciones y estados.

app/backend/bd/newsletter_source_store.py
    Reglas configurables de inclusión y exclusión.

app/backend/bd/message_content_store.py
    Contenido legible, enlaces y fechas de caducidad.

app/backend/services/gmail_ingestion.py
    Coordinación del escaneo, clasificación y procesamiento.

app/backend/services/message_content.py
    Descarga y decodificación segura de contenido MIME.

app/backend/services/content_parser.py
    Selección de texto legible y normalización de enlaces.

app/backend/services/message_content_processing.py
    Pipeline de decodificación, parsing y persistencia.

app/backend/services/oauth_credentials.py
    Validación, renovación y revocación de credenciales.

app/frontend/templates/
    Pantallas de inicio y dashboard.

tests/
    Pruebas unitarias, de integración, almacenamiento y rutas Flask.
```

## Instalación local

### 1. Crear el entorno virtual

```bash
python3 -m venv myvenv
myvenv/bin/python -m pip install -r requirements.txt
```

### 2. Preparar directorios locales

```bash
mkdir -p secrets data logs
```

### 3. Crear el archivo de entorno

```bash
cp .env.example .env
```

Edita `.env` con rutas y valores locales. No añadas secretos reales a
`.env.example`.

### 4. Generar claves para una instalación nueva

Estas órdenes son únicamente para una instalación nueva:

```bash
umask 077

myvenv/bin/python -c \
  "import secrets; print(secrets.token_hex(32))" \
  > secrets/flask_secret.key

myvenv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > secrets/token_encryption.key
```

Protege los archivos:

```bash
chmod 600 \
  secrets/flask_secret.key \
  secrets/token_encryption.key
```

No regeneres `token_encryption.key` si ya existen credenciales en la
base de datos. Una clave distinta no podrá descifrar los datos
anteriores.

## Configuración

Las variables admitidas están definidas en `.env.example`.

| Variable | Uso |
|---|---|
| `APP_ENV` | Entorno: normalmente `development` o `production`. |
| `APP_HOST` | Host utilizado por Flask. En local debe ser `localhost`. |
| `APP_PORT` | Puerto HTTP local. Por defecto, `5000`. |
| `APP_TIMEZONE` | Zona horaria IANA. Para el proyecto: `Europe/Madrid`. |
| `APP_DEBUG` | Activa o desactiva el modo debug. |
| `GOOGLE_APPLICATION_CREDENTIALS_WEB_APP` | Ruta al JSON OAuth de Google. |
| `GOOGLE_REDIRECT_URI` | Callback OAuth autorizado. |
| `GOOGLE_SCOPES` | Scopes separados por comas. |
| `FLASK_SECRET_KEY_PATH` | Archivo con la clave que firma la sesión Flask. |
| `TOKEN_ENCRYPTION_KEY_PATH` | Archivo con la clave Fernet. |
| `DATABASE_PATH` | Ruta de la base SQLite local. |
| `RETENTION_DAYS` | Días que se conservan el texto y los enlaces extraídos. La purga se ejecuta al inicio de cada escaneo. |

Ejemplo de rutas locales:

```ini
GOOGLE_APPLICATION_CREDENTIALS_WEB_APP=secrets/google_oauth_web.json
FLASK_SECRET_KEY_PATH=secrets/flask_secret.key
TOKEN_ENCRYPTION_KEY_PATH=secrets/token_encryption.key
DATABASE_PATH=data/newspulse.db
```

## Configuración de Google OAuth

En el cliente OAuth de Google Cloud configura:

```text
Authorized JavaScript origin:
http://localhost:5000

Authorized redirect URI:
http://localhost:5000/auth/google/callback
```

Utiliza siempre `localhost` durante todo el flujo. No mezcles
`localhost` y `127.0.0.1`, porque sus cookies pertenecen a hosts
diferentes.

El scope actual es:

```text
https://www.googleapis.com/auth/gmail.readonly
```

El envío del correo maestro requerirá añadir posteriormente
`gmail.send` y volver a autorizar la cuenta.

## Ejecutar la aplicación

```bash
myvenv/bin/python run_web_app.py
```

Abre:

```text
http://localhost:5000
```

El servidor incorporado de Flask es exclusivamente para desarrollo.

## Flujo de autenticación

```text
Navegador
    ↓
GET /auth/google
    ↓
Google OAuth
    ↓
GET /auth/google/callback
    ↓
validación de state
    ↓
credenciales cifradas con Fernet
    ↓
SQLite
    ↓
cookie firmada con account_id
    ↓
Gmail API
```

La cookie nunca almacena:

- Access token.
- Refresh token.
- Client secret.
- Credenciales OAuth completas.

La sesión contiene únicamente:

- `oauth_state` durante el proceso OAuth.
- `account_id` después de una autenticación correcta.

## Endpoints actuales

| Método | Ruta | Función |
|---|---|---|
| `GET` | `/` | Página de inicio. |
| `GET` | `/auth/google` | Inicia OAuth. |
| `GET` | `/auth/google/callback` | Procesa la respuesta de Google. |
| `GET` | `/dashboard` | Dashboard autenticado. |
| `GET` | `/api/gmail/profile` | Consulta el perfil Gmail. |
| `GET` | `/api/gmail/messages` | Lista hasta diez mensajes. |
| `GET` | `/api/newsletter-sources` | Lista las reglas de newsletters. |
| `POST` | `/api/newsletter-sources` | Crea o actualiza una regla. |
| `POST` | `/api/newsletter-sources/deactivate` | Desactiva una regla. |
| `POST` | `/api/ingestion/scan` | Ejecuta un escaneo incremental protegido. |
| `POST` | `/logout` | Elimina la sesión del navegador. |

Logout elimina la sesión local, pero no borra ni revoca las
credenciales almacenadas. Una función de desconexión permanente deberá
implementarse explícitamente si se necesita.

## Seguridad

- `.env`, claves, tokens, bases de datos, logs e imágenes generadas están
  excluidos de Git.
- Las credenciales OAuth se cifran antes de almacenarse en SQLite.
- La sesión Flask está firmada y no contiene credenciales.
- La cookie utiliza `HttpOnly` y `SameSite=Lax`.
- `Secure` se activa automáticamente cuando `APP_ENV=production`.
- El callback consume `oauth_state` una sola vez.
- Un identificador de cuenta inválido limpia la sesión y no se utiliza
  para consultar otra cuenta.
- Los errores OAuth no registran códigos, tokens ni mensajes sensibles.

En producción son obligatorios HTTPS, un servidor WSGI y una gestión
externa de secretos adecuada.

## Pruebas

Ejecuta toda la suite:

```bash
myvenv/bin/python -m unittest discover -s tests -v
```

La suite actual cubre:

- Cifrado, descifrado, renovación y revocación OAuth.
- Inicio y callback OAuth con validación de `state`.
- Sesión firmada y cuenta monousuario.
- Perfil y mensajes Gmail simulados.
- Cursor incremental y recuperación tras fallos parciales.
- Clasificación heurística y reglas configurables.
- Gestión web de fuentes de newsletters.
- Árboles MIME, adjuntos textuales y límites de tamaño.
- Parsing de texto plano y HTML.
- Normalización y deduplicación de enlaces.
- Persistencia, reintentos y estados de procesamiento.
- Retención y purga automática del contenido.
- Recorrido integral desde Gmail simulado hasta SQLite.
- Endpoints Flask y logout mediante `POST`.

Las pruebas no llaman realmente a Google ni utilizan secretos reales.

## Próxima fase

Los siguientes bloques de P1 completarán el correo maestro:

1. Traducir el contenido al español cuando sea necesario.
2. Resumir y sintetizar cada newsletter.
3. Generar una imagen representativa.
4. Construir el diseño HTML del correo maestro.
5. Enviar el resultado al mismo Gmail autenticado.
6. Programar la ejecución en la zona `Europe/Madrid`.
7. Definir y preparar el despliegue de producción.
