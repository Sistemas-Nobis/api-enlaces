from config import actualizar_token_wise
import pandas as pd

async def obtener_token_wise():
    # Verifica si el token está en el caché
    token = await actualizar_token_wise()
    return token


def buscar_usuario(agente_id):

    df = pd.read_excel("static/usuariosWise.xlsx")
    #print(f"Agente a buscar: {agente_id}")
    for index, row in df.iterrows():
        if str(row['ID']).strip() == str(agente_id).strip():
            #print(f"ID encontrado en la fila {index}: {row.to_dict()}")
            box = row['Box']
            return box
        else:
            pass
            #print(index, "-", row["ID"])
    else:
        print("Agente NO encontrado.")