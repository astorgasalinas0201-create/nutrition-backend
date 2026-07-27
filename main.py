import os
import io
import json
import asyncio
import httpx
from PIL import Image
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from supabase import create_client, Client

app = FastAPI(title="API Nutrición & Supermercados Chile")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONEXIÓN A SUPABASE ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if (SUPABASE_URL and SUPABASE_KEY) else None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

@app.get("/")
def inicio():
    return {"estado": "Servidor activo en Render 24/7 con Supabase 🔥"}

# --- RUTA DE IA: ANÁLISIS DE FOTOS CON GEMINI ---
@app.post("/api/analizar-plato")
async def analizar_plato(file: UploadFile = File(...)):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY no configurada")
    
    try:
        contenido_imagen = await file.read()
        imagen = Image.open(io.BytesIO(contenido_imagen))
        cliente = genai.Client(api_key=api_key)
        
        prompt = """
        Analiza detenidamente esta imagen de comida.
        1. Identifica el plato o los alimentos presentes.
        2. Estima la porción razonable en gramos para cada ingrediente visible.
        3. Calcula los macronutrientes totales (calorías, proteínas, carbohidratos, grasas).
        
        Responde ÚNICAMENTE con un objeto JSON válido sin bloques markdown:
        {
          "nombre_plato": "Nombre corto del plato",
          "porciones": [{"ingrediente": "Nombre", "gramos": 150}],
          "macros_totales": {"kcal": 450, "proteinas_g": 35, "carbohidratos_g": 40, "grasas_g": 15}
        }
        """
        
        respuesta = cliente.models.generate_content(
            model='gemini-2.5-flash',
            contents=[imagen, prompt]
        )
        
        texto_limpio = respuesta.text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_limpio)
    except Exception as e:
        return {"error": "No se pudo analizar la imagen", "detalle": str(e)}

# --- BÚSQUEDA DE PRECIOS CON CACHÉ DE SUPABASE ---
async def buscar_en_lider(cliente: httpx.AsyncClient, producto: str):
    try:
        url = f"https://apps.lider.cl/bff/search?query={producto}&page=1&facets="
        res = await cliente.get(url, headers=HEADERS, timeout=4.0)
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
    return {"supermercado": "Lider", "producto": f"{producto.title()} (Base)", "precio": 2490}

async def buscar_en_cencosud(cliente: httpx.AsyncClient, producto: str, marca: str):
    try:
        dominio = "jumbo.cl" if marca == "Jumbo" else "santaisabel.cl"
        url = f"https://www.{dominio}/api/catalog_system/pub/products/search/{producto}"
        res = await cliente.get(url, headers=HEADERS, timeout=4.0)
        if res.status_code == 200 and len(res.json()) > 0:
            p = res.json()[0]
            precio = p["items"][0]["sellers"][0]["commertialOffer"]["Price"]
            return {"supermercado": marca, "producto": p.get("productName"), "precio": precio}
    except Exception:
        pass
    return {"supermercado": marca, "producto": f"{producto.title()} (Base)", "precio": 2590 if marca == "Jumbo" else 2390}

@app.get("/api/precios")
async def obtener_precios(producto: str):
    busqueda_limpia = producto.lower().strip()
    
    # 1. Intentar leer desde la caché de Supabase
    if supabase:
        try:
            cache = supabase.table("precios_cache").select("*").eq("busqueda", busqueda_limpia).execute()
            if cache.data and len(cache.data) > 0:
                return {
                    "busqueda": busqueda_limpia,
                    "origen": "cache_supabase",
                    "resultados": [
                        {
                            "supermercado": item["supermercado"],
                            "producto": item["producto_nombre"],
                            "precio": item["precio"]
                        } for item in cache.data
                    ]
                }
        except Exception as e:
            print(f"Error consultando caché Supabase: {e}")

    # 2. Si no está en caché, buscar en tiempo real
    async with httpx.AsyncClient() as cliente:
        tareas = [
            buscar_en_lider(cliente, busqueda_limpia),
            buscar_en_cencosud(cliente, busqueda_limpia, "Jumbo"),
            buscar_en_cencosud(cliente, busqueda_limpia, "Santa Isabel")
        ]
        resultados = await asyncio.gather(*tareas)
    
    # 3. Guardar en Supabase para futuras consultas
    if supabase:
        try:
            para_insertar = [
                {
                    "busqueda": busqueda_limpia,
                    "supermercado": r.get("supermercado"),
                    "producto_nombre": r.get("producto"),
                    "precio": r.get("precio")
                } for r in resultados if r.get("precio") is not None
            ]
            if para_insertar:
                supabase.table("precios_cache").insert(para_insertar).execute()
        except Exception as e:
            print(f"Error guardando en caché Supabase: {e}")

    return {
        "busqueda": busqueda_limpia,
        "origen": "en_vivo",
        "resultados": resultados
    }
