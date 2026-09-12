# NewsPulse

NewsPulse es una aplicación personal que conecta una cuenta de Gmail y
prepara la base para recopilar, clasificar y resumir newsletters.

## Estado del proyecto

El bloque P0 estabiliza la autenticación y el acceso seguro a Gmail.

Actualmente están implementados:

- Aplicación web Flask.
- Autenticación OAuth 2.0 con Google.
- Validación de `state` frente a CSRF.
- Persistencia cifrada de credenciales OAuth con Fernet y SQLite.
- Renovación automática de credenciales caducadas.
- Sesión firmada con un identificador monousuario.
- Consulta del perfil de Gmail.
- Listado básico de mensajes.
- Logout mediante `POST`.
- Pruebas unitarias y de rutas Flask.

Todavía no están implementados:

- Detección de newsletters.
- Procesamiento incremental de mensajes.
- Traducción y resumen mediante IA.
- Generación de imágenes.
- Correo maestro semanal.
- Retención automática de datos.
- Programación y despliegue de producción.

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
    Aplicación Flask, rutas OAuth, sesión y endpoints Gmail.

app/backend/bd/db.py
    Inicialización y conexiones SQLite.

app/backend/bd/oauth_store.py
    Cifrado y persistencia de credenciales OAuth.

app/backend/services/oauth_credentials.py
    Validación, renovación y revocación de credenciales.

app/frontend/templates/
    Pantallas de inicio y dashboard.

tests/
    Pruebas del almacenamiento, renovación OAuth y rutas Flask.
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
| `RETENTION_DAYS` | Retención prevista. Se valida, pero todavía no se ejecuta automáticamente. |

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

- Cifrado y descifrado de credenciales.
- Renovación y revocación OAuth.
- Errores temporales.
- Inicio y callback OAuth.
- Validación de `state`.
- Sesión firmada y cuenta monousuario.
- Perfil y mensajes Gmail simulados.
- Logout mediante `POST`.

Las pruebas no llaman realmente a Google ni utilizan secretos reales.

## Próxima fase

P1 implementará el procesamiento incremental de newsletters:

1. Registrar el momento inicial de uso.
2. Consultar mensajes nuevos.
3. Detectar newsletters mediante reglas configurables.
4. Extraer texto y enlaces.
5. Traducir y resumir al español.
6. Generar una imagen por contenido.
7. Construir y enviar el correo maestro.
8. Aplicar la retención configurada de 30 días.
