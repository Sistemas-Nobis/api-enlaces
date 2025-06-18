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
import json
import re

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
    cursor.execute('''CREATE TABLE IF NOT EXISTS registros_llamador (
                        id VARCHAR(50) PRIMARY KEY,
                        caso_id VARCHAR(25) NOT NULL,
                        agente VARCHAR(25) NOT NULL,
                        nombre VARCHAR(50) NOT NULL,
                        dni VARCHAR(20) NOT NULL,
                        fecha VARCHAR(20) NOT NULL,
                        sucursal VARCHAR(50) NOT NULL,
                        bloqueado BOOLEAN DEFAULT FALSE,
                        llamado BOOLEAN DEFAULT FALSE,
                        box_llamado VARCHAR(10),
                        box_repite VARCHAR(10)
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
            if table_name not in ('log_alias', 'registros_llamador', 'usuarios', 'contador'):
                cursor.execute(f"SHOW COLUMNS FROM {table_name}")
                columns = [row[0] for row in cursor.fetchall()]
                if 'original_url' in columns:
                    query = f"SELECT original_url FROM {table_name} WHERE alias = %s"
                    cursor.execute(query, (alias,))
                    result = cursor.fetchone()
                    if result:
                        return result[0]
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

# Funciones para manejar los registros del prellamador en la base de datos
def obtener_ultimos_registros(sucursal=None, limit=5):
    conn = datos_conexion()
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    try:
        if sucursal:
            query = "SELECT * FROM registros_llamador WHERE sucursal = %s ORDER BY fecha DESC LIMIT %s"
            cursor.execute(query, (sucursal.lower(), limit))
        else:
            query = "SELECT * FROM registros_llamador ORDER BY fecha DESC LIMIT %s"
            cursor.execute(query, (limit,))
        
        registros = cursor.fetchall()
        # Convertir valores booleanos de MySQL (0/1) a Python (False/True)
        for registro in registros:
            registro['bloqueado'] = bool(registro['bloqueado'])
            registro['llamado'] = bool(registro['llamado'])
        
        return registros
    finally:
        cursor.close()
        conn.close()

def guardar_registro(registro):
    conn = datos_conexion()
    cursor = conn.cursor()
    try:
        query = """
        INSERT INTO registros_llamador 
        (id, caso_id, agente, nombre, dni, fecha, sucursal, bloqueado, llamado, box_llamado, box_repite) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(query, (
            registro["id"],
            registro["caso_id"],
            registro["agente"],
            registro["nombre"],
            registro["dni"],
            registro["fecha"],
            registro["sucursal"],
            registro.get("bloqueado", False),
            registro.get("llamado", False),
            registro.get("box_llamado", None),
            registro.get("box_repite", None)
        ))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error al guardar registro: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def actualizar_registro(registro_id, datos_actualizados):
    conn = datos_conexion()
    cursor = conn.cursor()
    try:
        # Construir la consulta de actualización dinámicamente
        campos = []
        valores = []
        
        for campo, valor in datos_actualizados.items():
            campos.append(f"{campo} = %s")
            valores.append(valor)
        
        # Añadir el ID al final de los valores
        valores.append(registro_id)
        
        query = f"UPDATE registros_llamador SET {', '.join(campos)} WHERE id = %s"
        cursor.execute(query, valores)
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        print(f"Error al actualizar registro: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def obtener_registro_por_id(registro_id):
    conn = datos_conexion()
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    try:
        query = "SELECT * FROM registros_llamador WHERE id = %s"
        cursor.execute(query, (registro_id,))
        registro = cursor.fetchone()
        if registro:
            # Convertir valores booleanos de MySQL (0/1) a Python (False/True)
            registro['bloqueado'] = bool(registro['bloqueado'])
            registro['llamado'] = bool(registro['llamado'])
        return registro
    finally:
        cursor.close()
        conn.close()

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
    #print(f"Nuevo llamador conectado para {key}. Total: {len(llamadores_activados[key])}")
    
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
            #print(f"Llamador desconectado de {key}. Restantes: {len(llamadores_activados[key])}")


# === WebSocket pre-llamador (para actualización en tiempo real) ===
@app.websocket("/ws/prellamador/{sucursal}")
async def websocket_prellamador(websocket: WebSocket, sucursal: str):
    await websocket.accept()
    
    # Agregar a la lista de websockets para esta sucursal
    if sucursal.lower() not in prellamadores_activados:
        prellamadores_activados[sucursal.lower()] = []
    
    prellamadores_activados[sucursal.lower()].append(websocket)
    
    # Enviar los últimos registros al conectarse
    registros = obtener_ultimos_registros(sucursal.lower())
    await websocket.send_json({
        "action": "initial_data",
        "registros": registros
    })
    
    try:
        # Mantenemos el socket abierto
        while True:
            data = await websocket.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
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

    if id_to_sucursal[1] == id_to_sucursal[id]:
        return templates.TemplateResponse("llamador_doble_sin_video.html", { # Casa central
            "request": request,
            "sucursal": id_to_sucursal[id]
        })
    elif id_to_sucursal[2] == id_to_sucursal[id]:
        return templates.TemplateResponse("llamador_doble.html", { # Santiago del estero
            "request": request,
            "sucursal": id_to_sucursal[id]
        })
    elif id_to_sucursal[3] == id_to_sucursal[id]:
        return templates.TemplateResponse("llamador_solo.html", { # Salta
            "request": request,
            "sucursal": id_to_sucursal[id]
        })
    elif id_to_sucursal[4] == id_to_sucursal[id]:
        return templates.TemplateResponse("llamador_doble.html", { # Catamarca
            "request": request,
            "sucursal": id_to_sucursal[id]
        })
    else:
        return templates.TemplateResponse("llamador_doble.html", {
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
    if not contenido or "[change_assign_agent_to]" not in contenido:
        return {"status": "invalido", "data": contenido or "Sin contenido"}
    
    # --- NUEVO BLOQUE ---
    try:
        contenido_json = json.loads(contenido)
        texto_actividad = contenido_json['activity'][0]['text']
    except Exception as e:
        return {"status": "invalido", "data": f"Error parseando contenido: {e}"}

    patron = r'(.+?)\s*\[change_assign_agent_to\]\s*(.+)'
    coincidencia = re.search(patron, texto_actividad)
    if not coincidencia:
        return {"status": "invalido", "data": "No se encontró el patrón esperado en el texto de actividad"}

    nombre_antes = coincidencia.group(1).strip()
    nombre_despues = coincidencia.group(2).strip()

    if nombre_antes.lower() != nombre_despues.lower():
        return {"status": "invalido", "data": f"Nombres distintos: '{nombre_antes}' vs '{nombre_despues}'"}
    # --- FIN NUEVO BLOQUE ---

    agente = data_wise.get("user_id")

    # Buscar sucursal
    url_actividades = f'https://api.wcx.cloud/core/v1/cases/{caso}/activities?fields=id,user_id,content,created_at,sending_status'
    response_actividades = requests.get(url_actividades, headers=headers_wise)
    data_actividades = response_actividades.json()

    sucursal = None
    for x in data_actividades:
        contenido_actividad = x.get('content', '').lower()
        if "casa central" in contenido_actividad:
            sucursal = "casa central"
            break
        elif "catamarca" in contenido_actividad:
            sucursal = "catamarca"
            break
        elif "salta" in contenido_actividad:
            sucursal = "salta"
            break
        elif "sgo del estero" in contenido_actividad:
            sucursal = "sgo del estero"
            break

    if not sucursal:
        return {"status": "invalido", "data": "sin contenido de sucursal", "agente": agente}

    # Obtener contacto
    contacto = data['contact_id']
    url_contacto = f'https://api.wcx.cloud/core/v1/contacts/{contacto}?fields=id,email,personal_id,phone,name,guid,password,custom_fields,last_update,organization_id,address'
    response_contacto = requests.get(url_contacto, headers=headers_wise)
    data_contacto = response_contacto.json()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    box = buscar_usuario(agente)

    nuevo_registro = {
        "id": uuid4().hex,
        "caso_id": data_wise["id"],
        "agente": agente,
        "nombre": re.sub(r'\d+', '', data_contacto["name"]).strip(),
        "dni": data_contacto["personal_id"],
        "fecha": now,
        "sucursal": sucursal.lower(),
        "bloqueado": True,
        "llamado": True,
        "box_llamado": box
    }

    if box:
        guardar_registro(nuevo_registro)
    else:
        raise HTTPException(status_code=405, detail="No se encontró box del agente. Rechazado.")

    key = f"box_{sucursal.lower()}"
    mensaje = {
        "id": nuevo_registro["id"],
        "name": nuevo_registro["nombre"],
        "dni": nuevo_registro["dni"],
        "fecha": nuevo_registro["fecha"],
        "descripcion": nuevo_registro["sucursal"],
        "box": box
    }

    # Enviar a llamadores activos
    if key in llamadores_activados:
        for ws in llamadores_activados[key]:
            try:
                await ws.send_json(mensaje)
            except:
                continue

    # (Opcional) seguir notificando a prellamadores solo para visibilidad
    if sucursal.lower() in prellamadores_activados:
        notificacion = {
            "action": "nuevo_registro",
            "registro": nuevo_registro
        }
        for ws in prellamadores_activados[sucursal.lower()]:
            try:
                await ws.send_json(notificacion)
            except:
                continue

    return {"status": "llamado_directo", "data": nuevo_registro}


# === GET: Pre-llamador con formulario y registros (mostrando todos) ===
@app.get("/pre-llamador/{id}", response_class=HTMLResponse)
def pre_llamador_get(request: Request, id: int):
    # Enviar TODOS los registros, no solo los no bloqueados
    registros = obtener_ultimos_registros(limit=10)
    return templates.TemplateResponse("prellamador.html", {"request": request, "registros": registros})

# === POST: Selecciona box/sucursal (solo guardar datos en sesión si aplica) ===
@app.post("/pre-llamador/{id}")
def pre_llamador_post(request: Request, id: int, box: str = Form(...), sucursal: str = Form(...)):
    # En este ejemplo no guardamos estado persistente del form
    return RedirectResponse("/pre-llamador/1", status_code=303)


# === POST: Llamar a un paciente (marca como llamado y envía a llamador) ===
# Modificar la función llamar_registro
@app.post("/llamar/{id}")
async def llamar_registro(request: Request, id: int, registro_id: str = Form(...), sucursal: str = Form(...)): #box: str = Form(...)
    registro = obtener_registro_por_id(registro_id)
    if registro and not registro.get("llamado", False):
        # Marcar como llamado
        #reg["llamado"] = True
        #reg["bloqueado"] = True

        agente_id = registro["agente"]
        #reg["box_llamado"] = box

        box = buscar_usuario(agente_id)
        datos_actualizados = {
            "llamado": True,
            "bloqueado": True,
            "box_llamado": box
        }
        actualizar_registro(registro_id, datos_actualizados)
        
        # Actualizar el registro con los nuevos datos
        registro.update(datos_actualizados)

        # Enviar al llamador por WebSocket
        key = f"box_{sucursal.lower()}"
        mensaje = {
            "id": registro["id"],
            "name": registro["nombre"],
            "dni": registro["dni"],
            "fecha": registro["fecha"],
            "descripcion": registro["sucursal"],
            "box": box
        }
            
        # Enviar a TODOS los llamadores conectados para esta sucursal
        if key in llamadores_activados and llamadores_activados[key]:
            websockets_con_error = []
            for idx, ws in enumerate(llamadores_activados[key]):
                try:
                    await ws.send_json(mensaje)
                    #print(f"Mensaje enviado a llamador {idx+1} de {key}")
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
                "registro": registro
            }
            for ws in prellamadores_activados[sucursal.lower()]:
                try:
                    await ws.send_json(notificacion)
                except:
                    continue
                
    return RedirectResponse("/pre-llamador/1", status_code=303)


@app.post("/repetir-llamado/{id}")
async def repetir_llamado(
    request: Request,
    id: int,
    registro_id: str = Form(...),
    box: str = Form(...),
    sucursal: str = Form(...)
):
    #print(f"Repetir llamado recibido: registro_id={registro_id}, box={box}, sucursal={sucursal}")

    # Validar entrada
    if not registro_id or not box or not sucursal:
        raise HTTPException(status_code=400, detail="Faltan parámetros requeridos: registro_id, box o sucursal")

    # Buscar el registro en la base de datos
    registro = obtener_registro_por_id(registro_id)
    
    if not registro:
        print(f"No se encontró el registro con ID {registro_id}")
        raise HTTPException(status_code=404, detail=f"Registro con ID {registro_id} no encontrado")

    # Actualizar el box de llamado en la base de datos
    actualizar_registro(registro_id, {"llamado": True, "bloqueado": True, "box_repite": box})
    
    # Actualizar el registro con el nuevo box
    #registro["box_llamado"] = box

    # Volver a obtener el registro actualizado
    registro = obtener_registro_por_id(registro_id)

    # Enviar a TODOS los llamadores activos de la sucursal
    key = f"box_{sucursal.lower()}"
    mensaje = {
        "id": registro["id"],
        "name": registro["nombre"],
        "dni": registro["dni"],
        "fecha": registro["fecha"],
        "descripcion": registro["sucursal"],
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
            #print(f"Mensaje enviado a llamador {idx+1} de {key}")
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
            "registro": registro
        }
        for prellamador_ws in prellamadores_activados[sucursal.lower()]:
            try:
                await prellamador_ws.send_json(notificacion)
            except Exception as e:
                print(f"Error al notificar a pre-llamador: {e}")
                continue

    #print(f"Repetición de llamado exitosa para registro {registro_id}")
    return RedirectResponse(f"/pre-llamador/{id}", status_code=303)


@app.get("/diagnostico/{id}")
async def diagnostico(id: int):
    """Ruta para diagnosticar las conexiones WebSocket activas"""
    resultado = {
        "llamadores": {},
        "prellamadores": {},
        "registros_db": len(obtener_ultimos_registros(limit=100))
    }
    
    # Contar llamadores activos
    for key, conexiones in llamadores_activados.items():
        resultado["llamadores"][key] = len(conexiones)
    
    # Contar prellamadores activos
    for key, conexiones in prellamadores_activados.items():
        resultado["prellamadores"][key] = len(conexiones)
    
    return resultado


@app.websocket("/ws/llamador-inicial/{sucursal}")
async def websocket_llamador_inicial(websocket: WebSocket, sucursal: str):
    await websocket.accept()
    registros = obtener_ultimos_registros(sucursal.lower(), limit=20)  # Aumenta el límite para asegurar que tienes los más recientes de cada box

    # Filtra solo los que tienen llamado y box_llamado
    llamados = [r for r in registros if r["llamado"] and r["box_llamado"]]

    # Quedarse solo con el registro más reciente por box_llamado
    recientes_por_box = {}
    for r in llamados:
        box = r["box_llamado"]
        # Si no está o este registro es más reciente, lo guardamos
        if box not in recientes_por_box or r["llamado"] > recientes_por_box[box]["llamado"]:
            recientes_por_box[box] = r

    # Ordena por fecha de llamado descendente (más reciente primero)
    resultado = sorted(recientes_por_box.values(), key=lambda x: x["llamado"], reverse=True)

    await websocket.send_json({
        "action": "historico",
        "registros": resultado
    })

    while True:
        try:
            data = await websocket.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
        except WebSocketDisconnect:
            break


@app.post("/liberar-llamado/{id}")
async def liberar_llamado(id: int, registro_id: str = Form(...), sucursal: str = Form(...)):
    # Actualizar el registro: dejarlo disponible
    actualizar_registro(registro_id, {"llamado": False, "bloqueado": False, "box_repite": None})

    registro = obtener_registro_por_id(registro_id)

    # Notificar a prellamadores para refrescar la tabla
    if sucursal.lower() in prellamadores_activados:
        notificacion = {
            "action": "actualizar_registro",
            "registro": registro
        }
        for ws in prellamadores_activados[sucursal.lower()]:
            try:
                await ws.send_json(notificacion)
            except:
                continue
            

    # Notificar a llamadores para que eliminen el registro de la vista
    key = f"box_{sucursal.lower()}"
    if key in llamadores_activados:
        notificacion = {
            "action": "eliminar_registro",
            "registro_id": registro_id
        }
        for ws in llamadores_activados[key]:
            try:
                await ws.send_json(notificacion)
            except:
                continue

    return {"status": "liberado"}