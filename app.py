from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
import pymysql
from urllib.parse import urljoin
from datetime import datetime

app = FastAPI(docs_url=None, redoc_url=None)

@app.get("/")
def read_root():
    return {"OK"}


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