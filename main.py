import os
import io
import json
import asyncio
import httpx
from PIL import Image
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai

app = FastAPI(title="API Nutrición & Supermercados Chile")

# Habilitar CORS para peticiones desde React Native / Expo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# --- RUTA PRINCIPAL ---
@app.get("/")
def inicio():
    return {"estado": "Servidor activo en Render 24/7 🔥"}

# --- RUTA DE IA: ANÁLISIS DE FOTOS CON GEMINI ---
@app.post("/api/analizar-plato")
async def analizar_plato(file: UploadFile = File(...)):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY no configurada en Render")
    
    try:
        # 1. Leer la imagen enviada desde la app
        contenido_imagen = await file.read()
        imagen = Image.open(io.BytesIO(contenido_imagen))
        
        # 2. Inicializar cliente de Gemini
        cliente = genai.Client(api_key=api_key)
        
        prompt = """
        Analiza detenidamente esta imagen de comida.
        1. Identifica el plato o los alimentos presentes.
        2. Estima la porción razonable en gramos para cada ingrediente visible.
        3. Calcula los macronutrientes totales (calorías, proteínas, carbohidratos, grasas).
        
        Responde ÚNICAMENTE con un objeto JSON válido (sin bloques de código markdown, sin ```json) con este formato exacto:
        {
          "nombre_plato": "Nombre corto del plato",
          "porciones": [
            {"ingrediente": "Nombre del ingrediente", "gramos": 150}
          ],
          "macros_totales": {
            "kcal": 450,
            "proteinas_g": 35,
            "carbohidratos_g": 40,
            "grasas_g": 15
          }
        }
        """
        
        # 3. Solicitar análisis a Gemini 2.5 Flash
        respuesta = cliente.models.generate_content(
            model='gemini-2.5-flash',
            contents=[imagen, prompt]
        )
        
        # 4. Limpiar texto y devolver JSON estructurado
        texto_limpio = respuesta.text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_limpio)
        
    except Exception as e:
        print(f"Error analizando foto: {e}")
        return {"error": "No se pudo analizar la imagen", "detalle": str(e)}

# --- BÚSQUEDA DE SUPERMERCADOS ---
async def buscar_en_lider(cliente: httpx.AsyncClient, producto: str):
    try:
        url = f"[https://apps.lider.cl/bff/search?query=](https://apps.lider.cl/bff/search?query=){producto}&page=1&facets="
        res = await cliente.get(url, headers=HEADERS, timeout=5.0)
        if res.status_code == 200:
            datos = res.json()
            if datos.get("products"):
                p = datos["products"][0]
                return {
                    "supermercado": "Lider",
                    "producto": p.get("displayName"),
                    "precio": p.get("price", {}).get("BasePriceSales")
                }
    except Exception:
        pass
    return {"supermercado": "Lider", "error": "No disponible"}

async def buscar_en_cencosud(cliente: httpx.AsyncClient, producto: str, marca: str):
    try:
        dominio = "jumbo.cl" if marca == "Jumbo" else "santaisabel.cl"
        url = f"https://www.{dominio}/api/catalog_system/pub/products/search/{producto}"
        res = await cliente.get(url, headers=HEADERS, timeout=5.0)
        if res.status_code == 200 and len(res.json()) > 0:
            p = res.json()[0]
            precio = p["items"][0]["sellers"][0]["commertialOffer"]["Price"]
            return {"supermercado": marca, "producto": p.get("productName"), "precio": precio}
    except Exception:
        pass
    return {"supermercado": marca, "error": "No disponible"}

@app.get("/api/precios")
async def obtener_precios(producto: str):
    async with httpx.AsyncClient() as cliente:
        tareas = [
            buscar_en_lider(cliente, producto),
            buscar_en_cencosud(cliente, producto, "Jumbo"),
            buscar_en_cencosud(cliente, producto, "Santa Isabel")
        ]
        resultados = await asyncio.gather(*tareas)
    return {"busqueda": producto, "resultados": resultados}
