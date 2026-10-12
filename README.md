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
| `LOG_DIRECTORY` | Directorio de logs. Las rutas relativas se resuelven desde la raíz del proyecto. Por defecto, `logs`. |
| `LOG_MAX_BYTES` | Tamaño máximo de cada archivo activo antes de rotarlo. Por defecto, `10485760` (10 MB). |
| `LOG_BACKUP_COUNT` | Número máximo de copias numeradas conservadas por archivo. Por defecto, `5`. |
| `LOG_LEVEL` | Umbral de aplicación: `DEBUG`, `INFO`, `WARNING`, `ERROR` o `CRITICAL`. Por defecto, `INFO`. |
| `GOOGLE_APPLICATION_CREDENTIALS_WEB_APP` | Ruta al JSON OAuth de Google. |
| `GOOGLE_REDIRECT_URI` | Callback OAuth autorizado. |
| `GOOGLE_SCOPES` | Scopes separados por comas. |
| `FLASK_SECRET_KEY_PATH` | Archivo con la clave que firma la sesión Flask. |
| `TOKEN_ENCRYPTION_KEY_PATH` | Archivo con la clave Fernet. |
| `DATABASE_PATH` | Ruta de la base SQLite local. Su directorio padre debe estar dedicado al almacenamiento privado de News Pulse. |
| `SQLITE_BUSY_TIMEOUT_MS` | Tiempo máximo en milisegundos que SQLite espera a otro escritor. Por defecto, `5000`. |
| `INGESTION_LEASE_SECONDS` | Vigencia de cada latido del lease exclusivo de ingestión. Por defecto, `900`. |
| `RETENTION_DAYS` | Días que se conservan el texto y los enlaces extraídos. La purga se ejecuta al inicio de cada escaneo. |

Ejemplo de rutas locales:

```ini
GOOGLE_APPLICATION_CREDENTIALS_WEB_APP=secrets/google_oauth_web.json
FLASK_SECRET_KEY_PATH=secrets/flask_secret.key
TOKEN_ENCRYPTION_KEY_PATH=secrets/token_encryption.key
DATABASE_PATH=data/newspulse.db
SQLITE_BUSY_TIMEOUT_MS=5000
INGESTION_LEASE_SECONDS=900
```

En sistemas POSIX, la inicialización restringe automáticamente el directorio
padre de `DATABASE_PATH` a `0700` y el archivo SQLite a `0600`. No configures la
base directamente dentro de un directorio compartido, porque News Pulse trata
ese directorio como almacenamiento privado de la aplicación.

SQLite utiliza un esquema versionado mediante `PRAGMA user_version`. La versión
actual es `4`. Al arrancar, News Pulse aplica las migraciones pendientes en
orden y dentro de una única transacción, valida las columnas, índices y claves
foráneas resultantes, y rechaza bases creadas por una versión más reciente de la
aplicación. La versión 2 añade validación en SQLite para las fechas UTC, los
arrays JSON, la coherencia del ciclo de procesamiento y la relación entre el
contenido retenido y su clasificación `newsletter`. Una fila `processed` puede
permanecer sin contenido después de la purga de retención prevista. Las bases
heredadas sin versión solo se marcan como actuales si su estructura y sus datos
son compatibles.

La versión 3 incorpora historial operativo sin duplicar el cuerpo de los
correos. `ingestion_runs` registra cada intento, su resultado, cursores,
contadores y un código de error seguro. Los historiales de estado y
clasificación conservan las transiciones y decisiones mientras las tablas
originales continúan representando el estado actual. El cierre satisfactorio
de un escaneo y el avance de su cursor se confirman en una sola transacción.

La versión 4 activa el modo WAL, aplica `SQLITE_BUSY_TIMEOUT_MS` a cada conexión
y añade un lease renovable a `ingestion_runs`. Un índice parcial permite una
sola ejecución `running` por cuenta incluso entre varios procesos. Si el proceso
propietario termina abruptamente, el lease caduca; el siguiente intento marca
esa ejecución como `failed` con un código seguro y adquiere un lease nuevo sin
avanzar el cursor anterior. El bloqueo local del endpoint se conserva como una
optimización, pero la exclusión real reside en SQLite.

Este diseño permite varios procesos de aplicación sobre una misma base en un
único host. No convierte SQLite en almacenamiento distribuido: varias máquinas,
un sistema de archivos de red o un volumen compartido requieren una base de
datos y un mecanismo de coordinación diseñados para ese entorno.

Antes de instalar una versión que incluya nuevas migraciones, detén la
aplicación y crea una copia privada de SQLite en un almacenamiento cifrado. Las
copias bajo `data/backups/` y los archivos `*.db.bak*` están excluidos de Git,
pero siguen conteniendo información sensible y no deben conservarse sin una
política de retención.

## Logging y rotación

`AppLogger` centraliza los registros de la aplicación. Cada línea incluye fecha
y hora con milisegundos y offset de `APP_TIMEZONE`, nivel, componente, función y
un identificador de correlación para las peticiones HTTP. Los flujos de
ingestión añaden etapas, contadores y duraciones sin registrar contenido de
mensajes, credenciales ni cabeceras de autorización.

Los archivos se rotan al alcanzar `LOG_MAX_BYTES`. La copia `.1` es la más
reciente y se conservan como máximo `LOG_BACKUP_COUNT` copias numeradas por
archivo. La rotación conserva archivos existentes, añade nuevos registros al
archivo activo y sólo elimina copias numéricas que exceden la política; no
modifica archivos históricos con nombres personalizados.

En Linux, un bloqueo lateral por archivo coordina escrituras y rotaciones entre
procesos, incluido el reloader de desarrollo. En plataformas sin bloqueos POSIX
debe utilizarse un único proceso escritor o una arquitectura externa de cola y
agregación de logs.

Para depuración local basta con cambiar la configuración, sin modificar código:

```ini
LOG_LEVEL=DEBUG
```

El formateador enmascara patrones habituales de tokens, contraseñas, claves API,
cookies y autorización. Esta defensa complementa, pero no sustituye, la regla de
no enviar nunca datos sensibles o contenidos completos a los métodos de log.
Si el propio handler no puede escribir o rotar un archivo, el flujo de la
aplicación continúa y el diagnóstico alternativo omite íntegramente el registro
original para no saltarse la redacción central.

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
- En sistemas POSIX, el directorio de la base utiliza permisos `0700` y el
  archivo SQLite utiliza `0600`.
- La sesión Flask está firmada y no contiene credenciales.
- La cookie utiliza `HttpOnly` y `SameSite=Lax`.
- `Secure` se activa automáticamente cuando `APP_ENV=production`.
- El callback consume `oauth_state` una sola vez.
- Un identificador de cuenta inválido limpia la sesión y no se utiliza
  para consultar otra cuenta.
- Los errores OAuth no registran códigos, tokens ni mensajes sensibles.
- Los logs aplican filtrado de secretos y rotación con retención acotada.

El cifrado Fernet protege las credenciales OAuth, pero el texto extraído y los
identificadores de mensajes no se cifran individualmente dentro de SQLite. Para
protegerlos frente a la pérdida física del dispositivo o de una copia de
seguridad debe utilizarse cifrado de disco o de volumen.

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
- Lease de ingestión entre procesos, caducidad y recuperación tras caída.
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
