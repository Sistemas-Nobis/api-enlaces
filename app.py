from fastapi import FastAPI, Request, Form, WebSocket, Depends, HTTPException, WebSocketDisconnect
from fastapi.responses import RedirectResponse, HTMLResponse
import pymysql
from urllib.parse import urljoin
from datetime import datetime
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from uuid import uuid4
import requests
from funciones import obtener_token_wise, buscar_usuario
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend

# No Docs
app = FastAPI(docs_url=None, redoc_url=None)

@app.get("/")
def read_root():
    return {"OK"}

# Montar la carpeta static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuración de la conexión a MySQL
def datos_conexion():
    return pymysql.connect(
        host="10.2.0.7",
        user="api",
        password="nobisapi",
        database="enlaces"
    )


# Inicialización de la base de datos
def iniciar_db():
    conn = datos_conexion()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS autorizaciones
                      (alias VARCHAR(50) PRIMARY KEY, original_url TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS boletas
                      (alias VARCHAR(50) PRIMARY KEY, original_url TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS log_alias (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        alias VARCHAR(50) NOT NULL,
                        fecha_solicitud TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );''')
    conn.commit()
    cursor.close()
    conn.close()

iniciar_db()

# Configurar FastAPICache al crear la aplicación
FastAPICache.init(InMemoryBackend())

# Función para buscar el alias en todas las tablas
def buscar_alias(alias: str):
    conn = datos_conexion()
    cursor = conn.cursor()
    try:
        # Obtener todas las tablas de la base de datos
        cursor.execute("SHOW TABLES")
        tables = cursor.fetchall()

        # Recorrer cada tabla y buscar el alias
        for (table_name,) in tables:
            if table_name != 'log_alias':
                query = f"SELECT original_url FROM {table_name} WHERE alias = %s"
                cursor.execute(query, (alias,))
                result = cursor.fetchone()
                if result:
                    return result[0]  # Retorna la URL si se encuentra el alias
        return None  # Retorna None si no encuentra el alias en ninguna tabla
    finally:
        cursor.close()
        conn.close()


# Función para registrar el alias con la fecha y hora de la solicitud
def registrar_alias(alias: str):
    conn = datos_conexion()
    cursor = conn.cursor()
    try:
        # Inserta el alias y la fecha actual en la tabla registro_aliases
        cursor.execute("INSERT INTO log_alias (alias, fecha_solicitud) VALUES (%s, %s)", (alias, datetime.now()))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error al registrar el alias: {e}")
    finally:
        cursor.close()
        conn.close()


@app.get("/{alias}")
async def redireccionar(alias: str):
    original_url = buscar_alias(alias)
    if original_url:

        base_url = "https://api.nobis.com.ar/"  # Debes reemplazarlo por tu dominio o base URL
        original_url = urljoin(base_url, original_url)
        registrar_alias(alias)

        # Convertir a URL absoluta
        return RedirectResponse(url=original_url)
    else:
        raise HTTPException(status_code=404, detail="Alias no encontrado")


templates = Jinja2Templates(directory="templates")

registros_disponibles = []
llamadores_activados = {}  # clave: box_sucursal → websocket
prellamadores_activados = {}  # clave: sucursal → lista de websockets
websockets_conectados = []

# === WebSocket llamador ===
@app.websocket("/ws/{sucursal}")
async def websocket_llamador(websocket: WebSocket, sucursal: str):
    key = f"box_{sucursal.lower()}"
    await websocket.accept()
    
    # Inicializar la lista si no existe
    if key not in llamadores_activados:
        llamadores_activados[key] = []
    
    # Añadir este websocket a la lista
    llamadores_activados[key].append(websocket)
    print(f"Nuevo llamador conectado para {key}. Total: {len(llamadores_activados[key])}")
    
    try:
        # Mantener la conexión abierta
        while True:
            data = await websocket.receive_text()
            # Si recibimos 'ping', respondemos con 'pong'
            if data == 'ping':
                await websocket.send_text('pong')
    except Exception as e:
        print(f"Error en websocket llamador: {e}")
    finally:
        # Eliminar este websocket de la lista cuando se desconecta
        if key in llamadores_activados and websocket in llamadores_activados[key]:
            llamadores_activados[key].remove(websocket)
            print(f"Llamador desconectado de {key}. Restantes: {len(llamadores_activados[key])}")


# === WebSocket pre-llamador (para actualización en tiempo real) ===
@app.websocket("/ws/prellamador/{sucursal}")
async def websocket_prellamador(websocket: WebSocket, sucursal: str):
    await websocket.accept()
    
    # Agregar a la lista de websockets para esta sucursal
    if sucursal.lower() not in prellamadores_activados:
        prellamadores_activados[sucursal.lower()] = []
    
    prellamadores_activados[sucursal.lower()].append(websocket)
    
    try:
        # Mantenemos el socket abierto
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        # Eliminar de la lista cuando se desconecta
        if sucursal.lower() in prellamadores_activados:
            if websocket in prellamadores_activados[sucursal.lower()]:
                prellamadores_activados[sucursal.lower()].remove(websocket)



@app.get("/llamador/{id}", response_class=HTMLResponse)
def ver_llamador(request: Request, id: int):
    id_to_sucursal = {
        1: "casa central",
        2: "sgo del estero",
        3: "salta",
        4: "catamarca"
    }

    if id not in id_to_sucursal:
        raise HTTPException(status_code=404, detail="Sucursal no válida")

    return templates.TemplateResponse("llamador.html", {
        "request": request,
        "sucursal": id_to_sucursal[id]
    })

import re

# === Webhook ===
@app.post("/webhook/{id}")
async def recibir_webhook(request: Request, id: int, token: str = Depends(obtener_token_wise)):
    if id != 1:
        return {"status": "ignorado"}

    data = await request.json()

    headers_wise = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
        'x-api-key': 'be9dd08a9cd8422a9af1372a445ec8e4'
    }

    caso = data["case_id"]
    actividad = data["activity_id"]

    # Obtener datos de actividad
    url = f'https://api.wcx.cloud/core/v1/cases/{caso}/activities/{actividad}?fields=id,type,user_id,content,contact_from,contacts_to,attachments,created_at,sending_status,channel'
    response = requests.get(url, headers=headers_wise)
    data_wise = response.json()

    contenido = data_wise.get('content')
    if contenido:
        if "[change_assign_agent_to]" in data_wise.get('content'):
            print(data_wise.get("id"))
            agente = data_wise.get("user_id")
        else:
            return {"status": "invalido", "data": f"Contenido: {contenido}"}
    else:
        return {"status": "invalido", "data": "Sin contenido"}

    url_actividades = f'https://api.wcx.cloud/core/v1/cases/{caso}/activities?fields=id,user_id,content,created_at,sending_status'
    response_actividades = requests.get(url_actividades, headers=headers_wise)
    data_actividades = response_actividades.json()

    for x in data_actividades:
        #print(x)
        contenido_actividad = x.get('content')
        if contenido_actividad:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            contenido_lower = contenido_actividad.lower()

            if "casa central" in contenido_lower:
                sucursal = "casa central"
                break
            elif "catamarca" in contenido_lower:
                sucursal = "catamarca"
                break
            elif "salta" in contenido_lower:
                sucursal = "salta"
                break
            elif "sgo del estero" in contenido_lower:
                sucursal = "sgo del estero"
                break
            else:
                pass
                
        else:
            pass
    
    if not sucursal:
        return {"status": "invalido", "data": "sin contenido de sucursal", "agente": agente}
    
    # Obtener datos del contacto
    contacto = data['contact_id']
    url_contacto = f'https://api.wcx.cloud/core/v1/contacts/{contacto}?fields=id,email,personal_id,phone,name,guid,password,custom_fields,last_update,organization_id,address'
    response_contacto = requests.get(url_contacto, headers=headers_wise)
    data_contacto = response_contacto.json()

    nuevo_registro = {
        "id": uuid4().hex,
        "caso_id": data_wise["id"],
        "agente": agente,
        "nombre": re.sub(r'\d+', '', data_contacto["name"]).strip(),
        "dni": data_contacto["personal_id"],
        "fecha": now,
        "sucursal": sucursal.lower(),
        "bloqueado": False,
        "llamado": False
    }

    registros_disponibles.append(nuevo_registro)
    #print(registros_disponibles)

    # Notificar a todos los pre-llamadores conectados para esta sucursal
    if sucursal.lower() in prellamadores_activados:
        notificacion = {
            "action": "nuevo_registro",
            "registro": nuevo_registro
        }
        for ws in prellamadores_activados[sucursal.lower()]:
            try:
                await ws.send_json(notificacion)
            except:
                continue  # Si falla, continuamos con el siguiente websocket


    return {"status": "registrado", "data": nuevo_registro}


# === GET: Pre-llamador con formulario y registros (mostrando todos) ===
@app.get("/pre-llamador/{id}", response_class=HTMLResponse)
def pre_llamador_get(request: Request, id: int):
    # Enviar TODOS los registros, no solo los no bloqueados
    return templates.TemplateResponse("prellamador.html", {"request": request, "registros": registros_disponibles})

# === POST: Selecciona box/sucursal (solo guardar datos en sesión si aplica) ===
@app.post("/pre-llamador/{id}")
def pre_llamador_post(request: Request, id: int, box: str = Form(...), sucursal: str = Form(...)):
    # En este ejemplo no guardamos estado persistente del form
    return RedirectResponse("/pre-llamador/1", status_code=303)


# === POST: Llamar a un paciente (marca como llamado y envía a llamador) ===
# Modificar la función llamar_registro
@app.post("/llamar/{id}")
async def llamar_registro(request: Request, id: int, registro_id: str = Form(...), sucursal: str = Form(...)): #box: str = Form(...)
    for reg in registros_disponibles:
        if reg["id"] == registro_id and not reg.get("llamado", False):
            # Marcar como llamado
            reg["llamado"] = True
            reg["bloqueado"] = True
            agente_id = reg["agente"]
            #reg["box_llamado"] = box

            box = buscar_usuario(agente_id)
            
            # Enviar al llamador por WebSocket
            key = f"box_{sucursal.lower()}"
            mensaje = {
                "id": reg["id"],
                "name": reg["nombre"],
                "dni": reg["dni"],
                "fecha": reg["fecha"],
                "descripcion": reg["sucursal"],
                "box": box
            }
            
            # Enviar a TODOS los llamadores conectados para esta sucursal
            if key in llamadores_activados and llamadores_activados[key]:
                websockets_con_error = []
                for idx, ws in enumerate(llamadores_activados[key]):
                    try:
                        await ws.send_json(mensaje)
                        print(f"Mensaje enviado a llamador {idx+1} de {key}")
                    except Exception as e:
                        print(f"Error al enviar a llamador {idx+1}: {e}")
                        websockets_con_error.append(ws)
                
                # Limpiar websockets con error
                for ws in websockets_con_error:
                    if ws in llamadores_activados[key]:
                        llamadores_activados[key].remove(ws)
            
            # Notificar a los pre-llamadores (código existente)
            if sucursal.lower() in prellamadores_activados:
                notificacion = {
                    "action": "actualizar_registro",
                    "registro": reg
                }
                for ws in prellamadores_activados[sucursal.lower()]:
                    try:
                        await ws.send_json(notificacion)
                    except:
                        continue
                
            break
    return RedirectResponse("/pre-llamador/1", status_code=303)


@app.post("/repetir-llamado/{id}")
async def repetir_llamado(
    request: Request,
    id: int,
    registro_id: str = Form(...),
    box: str = Form(...),
    sucursal: str = Form(...)
):
    print(f"Repetir llamado recibido: registro_id={registro_id}, box={box}, sucursal={sucursal}")

    # Validar entrada
    if not registro_id or not box or not sucursal:
        raise HTTPException(status_code=400, detail="Faltan parámetros requeridos: registro_id, box o sucursal")

    # Buscar el registro existente
    registro_encontrado = None
    for reg in registros_disponibles:
        if reg["id"] == registro_id:
            registro_encontrado = reg
            break

    if not registro_encontrado:
        print(f"No se encontró el registro con ID {registro_id}")
        raise HTTPException(status_code=404, detail=f"Registro con ID {registro_id} no encontrado")

    # Actualizar el box de llamado
    registro_encontrado["box_llamado"] = box

    # Enviar a TODOS los llamadores activos de la sucursal
    key = f"box_{sucursal.lower()}"
    mensaje = {
        "id": registro_encontrado["id"],
        "name": registro_encontrado["nombre"],
        "dni": registro_encontrado["dni"],
        "fecha": registro_encontrado["fecha"],
        "descripcion": registro_encontrado["sucursal"],
        "box": box,
        "repetido": True
    }
    
    # Verificar si hay llamadores conectados
    if key not in llamadores_activados or not llamadores_activados[key]:
        print(f"No se encontraron llamadores para el box {key}")
        raise HTTPException(status_code=503, detail=f"No hay llamadores activos para la sucursal {sucursal}")
    
    # Enviar a todos los llamadores conectados
    websockets_con_error = []
    for idx, ws in enumerate(llamadores_activados[key]):
        try:
            await ws.send_json(mensaje)
            print(f"Mensaje enviado a llamador {idx+1} de {key}")
        except Exception as e:
            print(f"Error al enviar a llamador {idx+1}: {e}")
            websockets_con_error.append(ws)
    
    # Limpiar websockets con error
    for ws in websockets_con_error:
        if ws in llamadores_activados[key]:
            llamadores_activados[key].remove(ws)

    # Notificar a los pre-llamadores conectados sobre el cambio
    if sucursal.lower() in prellamadores_activados:
        notificacion = {
            "action": "actualizar_registro",
            "registro": registro_encontrado
        }
        for prellamador_ws in prellamadores_activados[sucursal.lower()]:
            try:
                await prellamador_ws.send_json(notificacion)
            except Exception as e:
                print(f"Error al notificar a pre-llamador: {e}")
                continue

    print(f"Repetición de llamado exitosa para registro {registro_id}")
    return RedirectResponse(f"/pre-llamador/{id}", status_code=303)


@app.get("/diagnostico/{id}")
async def diagnostico(id: int):
    """Ruta para diagnosticar las conexiones WebSocket activas"""
    resultado = {
        "llamadores": {},
        "prellamadores": {}
    }
    
    # Contar llamadores activos
    for key, conexiones in llamadores_activados.items():
        resultado["llamadores"][key] = len(conexiones)
    
    # Contar prellamadores activos
    for key, conexiones in prellamadores_activados.items():
        resultado["prellamadores"][key] = len(conexiones)
    
    return resultado