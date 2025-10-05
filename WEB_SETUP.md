# Smart Newsletters - Web Application Setup

## Descripción

Se ha creado una aplicación web Flask que permite autenticación con Google OAuth y acceso a la API de Gmail. La aplicación integra con tu sistema existente de autenticación Gmail.

## Archivos Creados/Modificados

### Nuevos Archivos:
- `app/web_app.py` - Aplicación Flask principal con OAuth
- `app/templates/index.html` - Página de inicio con botón de Google Sign-In
- `app/templates/dashboard.html` - Dashboard después de autenticación
- `run_web_app.py` - Script para ejecutar la aplicación web
- `WEB_SETUP.md` - Esta documentación

### Archivos Modificados:
- `requirements.txt` - Agregado Flask
- `index.html` - Redirige a la aplicación web
- `app/main.py` - Agregada función para crear servicio Gmail desde credenciales web

## Configuración Requerida

### 1. Variables de Entorno
Asegúrate de que tu archivo `.env` en `config/` tenga:
```
GOOGLE_APPLICATION_CREDENTIALS_DESKTOP_APP=/ruta/a/tu/client_secret.json
SECRET_KEY=tu-clave-secreta-para-flask
```

### 2. Credenciales de Google OAuth
Tu archivo `client_secret.json` debe estar configurado para aplicaciones web con:
- **Authorized JavaScript origins**: `http://localhost:5000`
- **Authorized redirect URIs**: `http://localhost:5000/auth/google/callback`

## Instalación y Ejecución

### 1. Instalar Dependencias
```bash
pip install -r requirements.txt
```

### 2. Ejecutar la Aplicación Web
```bash
python run_web_app.py
```

### 3. Acceder a la Aplicación
- Abre tu navegador en: `http://localhost:5000`
- Haz clic en "Iniciar sesión con Google"
- Autoriza la aplicación
- Serás redirigido al dashboard

## Funcionalidades

### Página de Inicio (`/`)
- Botón de Google Sign-In
- Diseño moderno y responsivo
- Manejo de errores de autenticación

### Dashboard (`/dashboard`)
- Información del usuario autenticado
- Botones para acceder a funcionalidades Gmail
- Visualización de perfil Gmail
- Lista de mensajes recientes

### API Endpoints
- `GET /api/gmail/profile` - Obtiene perfil Gmail del usuario
- `GET /api/gmail/messages` - Obtiene mensajes recientes
- `GET /logout` - Cierra sesión

## Integración con tu Sistema Existente

La aplicación web utiliza las mismas clases que tu aplicación principal:

```python
# En web_app.py
from tools.gmail.gmail_client import GmailClient
from tools.gmail.gmail_authenticator import GmailAuthenticator

# Crear servicio Gmail desde credenciales OAuth
gmail_service = build('gmail', 'v1', credentials=credentials)
gmail_client = GmailClient(gmail_service)
```

### Función Helper en main.py
Se agregó `create_gmail_service_from_credentials()` que permite crear un servicio Gmail desde las credenciales OAuth de la sesión web.

## Seguridad

- Las credenciales se almacenan temporalmente en la sesión Flask
- En producción, considera usar almacenamiento más seguro (Redis, base de datos)
- La clave secreta de Flask debe ser cambiada en producción

## Desarrollo

### Estructura de Templates
```
app/templates/
├── index.html      # Página de inicio
└── dashboard.html  # Dashboard post-autenticación
```

### Logs
Los logs se guardan en `logs/web_app.log`

## Próximos Pasos

1. **Configurar HTTPS** para producción
2. **Implementar análisis de newsletters** en el dashboard
3. **Agregar gestión de suscripciones**
4. **Mejorar almacenamiento de credenciales** para múltiples usuarios
5. **Agregar más funcionalidades Gmail** (enviar emails, gestionar etiquetas, etc.)

## Solución de Problemas

### Error de Credenciales
- Verifica que `GOOGLE_APPLICATION_CREDENTIALS_DESKTOP_APP` apunte al archivo correcto
- Asegúrate de que las URIs de redirección estén configuradas en Google Console

### Error de Puerto
- Si el puerto 5000 está ocupado, cambia el puerto en `run_web_app.py`

### Error de Templates
- Verifica que el directorio `app/templates/` existe
- Asegúrate de que Flask puede encontrar los templates

