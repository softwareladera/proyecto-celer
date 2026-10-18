import flet as ft
import os
from flet import Icons
import libsql
import shutil
from datetime import datetime



# =====================================================================
# 1. PARTE: CONFIGURACIÓN Y FUNCIONES DE LA BASE DE DATOS (SQLITE)
# =====================================================================

def inicializar_base_de_datos():
    """Crea la conexión remota a Turso y las tablas si no existen."""
    # 1. Intentamos leer desde Vercel
    db_url = os.getenv("TURSO_DATABASE_URL")
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip('"' "'")
    
    conn = libsql.connect(database=db_url, auth_token=auth_token)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;") 


  
    # Tabla: Usuarios
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        rol TEXT CHECK(rol IN ('master', 'vendedor')) NOT NULL
    );
    """)
    cursor.execute("INSERT OR IGNORE INTO usuarios (username, password, rol) VALUES ('admin', 'admin123', 'master');")

    # Tabla: Productos (Inventario) - Modificada a REAL para peso y agregados campos de cajas/barras
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        codigo_barras TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        precio_compra REAL NOT NULL,
        precio_venta REAL NOT NULL,
        stock REAL DEFAULT 0,
        stock_minimo REAL DEFAULT 5,
        unidad_medida TEXT CHECK(unidad_medida IN ('UNIDAD', 'KG', 'CAJA')) DEFAULT 'UNIDAD',
        contenido_caja INTEGER DEFAULT 1,
        precio_caja REAL DEFAULT 0
    );
    """)
    
    # === REEMPLAZA ÚNICAMENTE ESTA SECCIÓN EN TU FUNCIÓN ORIGINAL ===
    # Tabla: Ventas (Cabecera) - Modificada para incluir vendedor, referencia y conciliación
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ventas (
        id INTEGER PRIMARY KEY,
        fecha_hora TEXT DEFAULT (datetime('now', 'localtime')),
        total REAL NOT NULL,
        metodo_pago TEXT NOT NULL,
        vendedor TEXT,
        referencia_pm TEXT,
        conciliado INTEGER DEFAULT 0
    );
    """)
    
    # Tabla: Detalles de Ventas - Modificada cantidad a REAL y vinculada a codigo_barras
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS detalles_ventas (
        id INTEGER PRIMARY KEY,
        venta_id INTEGER NOT NULL,
        producto_codigo TEXT NOT NULL,
        cantidad REAL NOT NULL,
        precio_unitario REAL NOT NULL,
        es_caja INTEGER DEFAULT 0,
        FOREIGN KEY (venta_id) REFERENCES ventas(id) ON DELETE CASCADE,
        FOREIGN KEY (producto_codigo) REFERENCES productos(codigo_barras)
    );
    """)
    

    
    # Tabla: Flujo de Caja (Ingresos y Egresos manuales)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS flujo_caja (
        id INTEGER PRIMARY KEY ,
        fecha TEXT DEFAULT (date('now', 'localtime')),
        tipo TEXT CHECK(tipo IN ('INGRESO', 'EGRESO')) NOT NULL,
        categoria TEXT NOT NULL,
        descripcion TEXT,
        monto REAL NOT NULL
    );
    """)
    

    # Tabla: Configuración Global (Para guardar la tasa del dólar)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS configuracion (
        clave TEXT PRIMARY KEY,
        valor TEXT NOT NULL
    );
    """)
    # Insertar tasa inicial de 1.00 si la tabla está vacía
    cursor.execute("INSERT OR IGNORE INTO configuracion (clave, valor) VALUES ('tasa_dolar', '1.00');")

    
    conn.commit()
    return conn


    


# --- CONSULTAS SQL ESPECÍFICAS ---

def db_insertar_producto(codigo_barras, nombre, p_compra, p_venta, stock, stock_min, unidad_medida='UNIDAD', contenido_caja=1, precio_caja=0.0):
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Inserta o actualiza un producto basándose estrictamente en su código de barras.
    Soporta decimales para peso y configuraciones para cajas.
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    
    codigo_limpio = str(codigo_barras).strip()

    # Buscamos si el código de barras ya existe en el sistema
    cursor.execute("SELECT stock FROM productos WHERE codigo_barras = ?", (codigo_limpio,))
    existente = cursor.fetchone()
    
    if existente:
        # Si ya existe, actualiza los datos e incrementa el stock de forma decimal
        stock_actual = existente[0]
        nuevo_stock = float(stock_actual) + float(stock)
        cursor.execute("""
            UPDATE productos 
            SET nombre = ?, precio_compra = ?, precio_venta = ?, stock = ?, stock_minimo = ?, 
                unidad_medida = ?, contenido_caja = ?, precio_caja = ?
            WHERE codigo_barras = ?
        """, (nombre, p_compra, p_venta, nuevo_stock, stock_min, unidad_medida, contenido_caja, precio_caja, codigo_limpio))
    else:
        # Si es nuevo, lo registra directamente desde cero
        cursor.execute("""
            INSERT INTO productos (codigo_barras, nombre, precio_compra, precio_venta, stock, stock_minimo, unidad_medida, contenido_caja, precio_caja) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (codigo_limpio, nombre, p_compra, p_venta, float(stock), stock_min, unidad_medida, contenido_caja, precio_caja))
        
    conn.commit()
    conn.close()

def db_importar_inventario_excel(ruta_archivo_excel):
    """
    Lee un archivo Excel sin dependencias externas e inserta o incrementa 
    masivamente los productos en la base de datos unificada.
    """
    import zipfile
    import xml.etree.ElementTree as ET
    import sqlite3

    conn = inicializar_base_datos()
    cursor = conn.cursor()
    
    # CORRECCIÓN 1: Namespace oficial y completo de OpenXML / Excel
    ns = {"ns": "http://openxmlformats.org"}
    
    # Función auxiliar para convertir letras de Excel (A, B, AA) a índices (0, 1, 26) de forma segura
    def letra_a_indice(letra):
        num = 0
        for char in letra.upper():
            if char.isalpha():
                num = num * 26 + (ord(char) - ord('A') + 1)
        return num - 1

    try:
        cursor.execute("BEGIN TRANSACTION;")
        
        with zipfile.ZipFile(ruta_archivo_excel, 'r') as z:
            strings = []
            
            # Leer las cadenas de texto compartidas (Shared Strings)
            if "xl/sharedStrings.xml" in z.namelist():
                xml_content = z.read("xl/sharedStrings.xml")
                root_strings = ET.fromstring(xml_content)
                # Aplicamos el Namespace correcto en la búsqueda
                for t in root_strings.findall(".//ns:t", ns):
                    strings.append(t.text if t.text else "")

            # Leer la primera hoja de cálculo
            xml_content = z.read("xl/worksheets/sheet1.xml")
            root_sheet = ET.fromstring(xml_content)
            filas = root_sheet.findall(".//ns:row", ns)
            
            for idx, fila in enumerate(filas):
                if idx == 0:
                    continue  # Saltamos los títulos del Excel (Fila 1)
                
                valores_celdas = ["" for _ in range(9)]
                celdas = fila.findall("ns:c", ns)
                
                for celda in celdas:
                    ref = celda.get("r")
                    if not ref:
                        continue
                    
                    # Extraer solo las letras de la coordenada de la celda (Ej: "A2" -> "A")
                    letra_columna = "".join([char for char in ref if char.isalpha()])
                    idx_col = letra_a_indice(letra_columna)
                    
                    if 0 <= idx_col < 9:
                        tipo = celda.get("t")
                        valor_nodo = celda.find("ns:v", ns)
                        
                        if valor_nodo is not None:
                            val = valor_nodo.text
                            if tipo == "s" and val is not None:
                                val = strings[int(val)]
                            valores_celdas[idx_col] = val

                # Procesamos únicamente si la fila contiene código de barras y nombre
                if valores_celdas[0] and valores_celdas[1]:
                    cod_b = str(valores_celdas[0]).strip()
                    nom = str(valores_celdas[1]).strip()
                    pc = float(valores_celdas[2]) if valores_celdas[2] else 0.0
                    pv = float(valores_celdas[3]) if valores_celdas[3] else 0.0
                    st = float(valores_celdas[4]) if valores_celdas[4] else 0.0
                    sm = float(valores_celdas[5]) if valores_celdas[5] else 5.0
                    
                    u_medida = str(valores_celdas[6]).strip().upper() if valores_celdas[6] else "UNIDAD"
                    if u_medida not in ["UNIDAD", "KG", "CAJA"]:
                        u_medida = "UNIDAD"
                        
                    cont_c = int(valores_celdas[7]) if valores_celdas[7] else 1
                    pr_caja = float(valores_celdas[8]) if valores_celdas[8] else 0.0

                    # LÓGICA SQL: Suma stock si existe, o crea si es nuevo
                    cursor.execute("SELECT stock FROM productos WHERE codigo_barras = ?", (cod_b,))
                    existente = cursor.fetchone()
                    
                    if existente:
                        nuevo_stock = float(existente[0]) + st
                        cursor.execute("""
                            UPDATE productos 
                            SET nombre = ?, precio_compra = ?, precio_venta = ?, stock = ?, stock_minimo = ?, 
                                unidad_medida = ?, contenido_caja = ?, precio_caja = ?
                            WHERE codigo_barras = ?
                        """, (nom, pc, pv, nuevo_stock, sm, u_medida, cont_c, pr_caja, cod_b))
                    else:
                        cursor.execute("""
                            INSERT INTO productos (codigo_barras, nombre, precio_compra, precio_venta, stock, stock_minimo, unidad_medida, contenido_caja, precio_caja) 
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (cod_b, nom, pc, pv, st, sm, u_medida, cont_c, pr_caja))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error al importar archivo de inventario Excel: {e}")
        return False
    finally:
        conn.close()





def db_obtener_productos():
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    # Consulta directa y rápida de 9 columnas ordenadas por nombre
    cursor.execute("""
        SELECT codigo_barras, nombre, precio_compra, precio_venta, 
               stock, stock_minimo, unidad_medida, contenido_caja, precio_caja 
        FROM productos ORDER BY nombre ASC
    """)
    datos = cursor.fetchall()
    conn.close()
    return datos


def db_obtener_todos_usuarios():
    """Trae la lista de todos los usuarios registrados en el sistema para poder listarlos."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    usuarios = []
    try:
        # Traemos el ID, el nombre de usuario y el rol ('master' o 'vendedor')
        cursor.execute("SELECT id, username, rol FROM usuarios ORDER BY id DESC")
        usuarios = cursor.fetchall()
    except Exception as e:
        print(f"Error al obtener la lista de usuarios: {e}")
        usuarios = []
    finally:
        conn.close()
    return usuarios


def db_obtener_ventas_por_vendedor_historico(fecha_seleccionada=None):
    """Suma las ventas en $ por cada vendedor """
    
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    reporte = {}
    try:
        query = "SELECT vendedor, SUM(total) FROM ventas"
        params = []
        
        # SOLUCIÓN: Usamos datetime(fecha_hora) 
        if fecha_seleccionada:
            query += " WHERE DATE(datetime(fecha_hora)) = DATE(?)"
            params.append(fecha_seleccionada)
            
        query += " GROUP BY vendedor ORDER BY SUM(total) DESC"
        
        cursor.execute(query, params)
        for fila in cursor.fetchall():
            vendedor = str(fila[0]).upper() if fila[0] else "ADMIN"
            monto_total = float(fila[1] or 0.0)
            
            if vendedor in reporte:
                reporte[vendedor] += monto_total
            else:
                reporte[vendedor] = monto_total
    except Exception as e:
        print(f"Error al obtener ventas por vendedor en historial: {e}")
    finally:
        conn.close()
    return reporte




def db_eliminar_venta_y_reponer_inventario(id_venta):
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Elimina la venta y repone el stock de forma exacta sumando kilos decimales
    o multiplicando las cajas devueltas por su contenido interno.
    """
    conn = inicializar_base_datos() 
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION;")

        # 1. Obtener el código de barras, cantidad vendida y datos de caja cruzando con productos
        cursor.execute("""
            SELECT dv.producto_codigo, dv.cantidad, dv.es_caja, p.contenido_caja 
            FROM detalles_ventas dv
            JOIN productos p ON dv.producto_codigo = p.codigo_barras
            WHERE dv.venta_id = ?
        """, (id_venta,))
        articulos = cursor.fetchall()

        # 2. Devolver las cantidades reales calculadas al stock global
        for prod_codigo, cantidad, es_caja, contenido_caja in articulos:
            if es_caja == 1:
                # Si se devolvió una caja, sumamos el equivalente en unidades individuales
                unidades_reponer = float(cantidad) * int(contenido_caja)
            else:
                # Si fue por kilo o unidad regular, se devuelve la cantidad decimal directa
                unidades_reponer = float(cantidad)
                
            cursor.execute("""
                UPDATE productos 
                SET stock = stock + ? 
                WHERE codigo_barras = ?
            """, (unidades_reponer, prod_codigo))

        # 3. Eliminar los detalles de la venta
        cursor.execute("DELETE FROM detalles_ventas WHERE venta_id = ?;", (id_venta,))

        # 4. Eliminar la cabecera de la venta principal
        cursor.execute("DELETE FROM ventas WHERE id = ?;", (id_venta,))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error al eliminar la venta {id_venta} e intentar reponer inventario: {e}")
        return False
    finally:
        conn.close()






def db_eliminar_usuario(id_usuario):
    """Elimina de forma permanente un usuario por su ID, impidiendo borrar la cuenta 'admin'."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    # Protección extra para que el administrador no se auto-elimine por accidente
    cursor.execute("DELETE FROM usuarios WHERE id = ? AND username != 'admin'", (id_usuario,))
    conn.commit()
    conn.close()



def db_verificar_credenciales(username, password):
    """Verifica si el usuario existe y la contraseña coincide. Retorna (rol, username) o None."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("SELECT rol, username FROM usuarios WHERE LOWER(username) = LOWER(?) AND password = ?", (username, password))
    res = cursor.fetchone()
    conn.close()
    return res if res else None

def db_obtener_vendedores():
    """Retorna la lista de usuarios que tienen el rol de vendedor."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username FROM usuarios WHERE rol = 'vendedor' ORDER BY username ASC")
    datos = cursor.fetchall()
    conn.close()
    return datos



def db_insertar_usuario(username, password, rol):
    """Registra un nuevo usuario en el sistema con Turso."""
    conn = None
    try:
        conn = inicializar_base_datos()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)", (username, password, rol))
        conn.commit()
        return True
    # Captura cualquier error de Base de Datos (así no dependes de sqlite3)
    except Exception as e:
        # Si el error menciona que ya existe o está duplicado (IntegrityError de Turso)
        if "UNIQUE" in str(e) or "already exists" in str(e):
            return False  # El usuario ya existe
        raise e # Si es otro error diferente, lo muestra en consola para que lo veas
    finally:
        if conn:
            conn.close()



def db_actualizar_conciliacion(id_venta, estado):
    """Actualiza el estado de verificación en el banco (1 para tildado, 0 para destildar)"""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE ventas SET conciliado = ? WHERE id = ?", (int(estado), int(id_venta)))
        conn.commit()
    except Exception as e:
        print(f"Error al actualizar conciliación: {e}")
    finally:
        conn.close()




def db_obtener_lista_vendedores():
    """Trae la lista de todos los nombres de usuario registrados."""
    try:
        conn = inicializar_base_datos()
        cursor = conn.cursor()
        # Traemos solo los usernames ordenados alfabéticamente
        cursor.execute("SELECT username FROM usuarios ORDER BY username ASC;")
        filas = cursor.fetchall()
        conn.close()
        # Convertimos las tuplas de SQLite en una lista limpia de strings
        return [fila[0] for fila in filas]
    except Exception as e:
        print(f"Error al obtener vendedores: {e}")
        return []



def db_obtener_resumen(fecha_inicio=None, fecha_fin=None):
    """
    MANTIENE TU LOGICA ORIGINAL CORREGIDA.
    Calcula ventas, egresos y alertas de stock, adaptándose de forma exacta 
    al nuevo esquema de inventario decimal (kilos y piezas).
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    
    # 1. CONSULTA DE VENTAS (Tus filtros de tiempo originales se mantienen idénticos)
    if fecha_inicio and fecha_fin:
        query_ventas = "SELECT SUM(total) FROM ventas WHERE date(fecha_hora) BETWEEN ? AND ?;"
        cursor.execute(query_ventas, (fecha_inicio, fecha_fin))
    else:
        query_ventas = "SELECT SUM(total) FROM ventas;"
        cursor.execute(query_ventas)
        
    res_v = cursor.fetchone()
    total_ventas = res_v[0] if res_v and res_v[0] is not None else 0.0
    
    # 2. CONSULTA DE EGRESOS
    if fecha_inicio and fecha_fin:
        query_egresos = "SELECT SUM(monto) FROM flujo_caja WHERE tipo = 'EGRESO' AND date(fecha) BETWEEN ? AND ?;"
        cursor.execute(query_egresos, (fecha_inicio, fecha_fin))
    else:
        query_egresos = "SELECT SUM(monto) FROM flujo_caja WHERE tipo = 'EGRESO';"
        cursor.execute(query_egresos)
        
    res_e = cursor.fetchone()
    total_egresos = res_e[0] if res_e and res_e[0] is not None else 0.0
    
    # 3. ALERTA DE STOCK MODIFICADA
    # Forzamos a SQLite a evaluar los campos como números reales (REAL) para evitar fallas por decimales flotantes en Kg
    cursor.execute("SELECT COUNT(*) FROM productos WHERE CAST(stock AS REAL) <= CAST(stock_minimo AS REAL);")
    res_a = cursor.fetchone()
    p_alerta = res_a[0] if res_a and res_a[0] is not None else 0
    
    conn.close()
    return total_ventas, total_egresos, (total_ventas - total_egresos), p_alerta



def db_actualizar_producto(codigo_barras, nombre, p_compra, p_venta, stock, stock_min, unidad_medida='UNIDAD', contenido_caja=1, precio_caja=0.0):
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Actualiza un producto basándose en su código de barras único.
    Soporta stock decimal (Kg) y los parámetros correspondientes para cajas.
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE productos 
        SET nombre = ?, precio_compra = ?, precio_venta = ?, stock = ?, stock_minimo = ?, 
            unidad_medida = ?, contenido_caja = ?, precio_caja = ?
        WHERE codigo_barras = ?
    """, (nombre, p_compra, p_venta, float(stock), float(stock_min), unidad_medida, contenido_caja, precio_caja, str(codigo_barras).strip()))
    conn.commit()
    conn.close()

def db_eliminar_producto(codigo_barras):
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Elimina permanentemente un producto del inventario usando su código de barras único.
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM productos WHERE codigo_barras = ?", (str(codigo_barras).strip(),))
    conn.commit()
    conn.close()




def db_buscar_producto_venta(termino):
    conn = inicializar_base_datos()

    
def db_registrar_venta_completa(carrito, total, metodo, vendedor_name, referencia_pm=None):
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Registra de forma atómica la cabecera, los detalles de la venta y descuenta
    el stock decimal (Kg o piezas) del inventario basándose en el código de barras.
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION;")
        vendedor_limpio = str(vendedor_name).upper()

        # 1. Insertar la cabecera de la venta limpia
        cursor.execute(
            "INSERT INTO ventas (total, metodo_pago, vendedor, referencia_pm, conciliado) VALUES (?, ?, ?, ?, 0)", 
            (float(total), metodo, vendedor_limpio, str(referencia_pm or ""))
        )
        v_id = cursor.lastrowid
        
        # 2. Procesar los artículos del carrito con la estructura compatible de objetos
        for item in carrito:
            # Estructura que le pasa la interfaz: [cod_barras, nombre, precio, cantidad, es_caja, descontar_unidades]
            cod_barras = str(item[0]).strip()
            precio_u = float(item[2])
            cantidad_vendida = float(item[3])
            es_caja = int(item[4])
            descontar_unidades = float(item[5])

            # Insertar en el detalle de la venta registrando la modalidad (es_caja) y el código de barras
            cursor.execute("""
                INSERT INTO detalles_ventas (venta_id, producto_codigo, cantidad, precio_unitario, es_caja) 
                VALUES (?, ?, ?, ?, ?)
            """, (v_id, cod_barras, cantidad_vendida, precio_u, es_caja))
            
            # Descuenta directamente del inventario único/global aplicando la cantidad equivalente
            cursor.execute("""
                UPDATE productos 
                SET stock = stock - ? 
                WHERE codigo_barras = ?
            """, (descontar_unidades, cod_barras))
            
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error en base de datos al registrar la venta completa: {e}")
        return False
    finally:
        conn.close()



def db_obtener_historial_ventas():
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Trae el historial completo de ventas de forma directa, rápida y limpia,
    aprovechando la estructura nativa fija de la nueva base de datos.
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    datos = []
    try:
        # Consulta limpia y directa sin reintentos innecesarios
        cursor.execute("""
            SELECT id, datetime(fecha_hora), total, metodo_pago, vendedor, referencia_pm, conciliado 
            FROM ventas 
            ORDER BY id DESC
        """)
        datos = cursor.fetchall()
    except Exception as e:
        print(f"Error en consulta de historial de ventas: {e}")
        datos = []
    finally:
        conn.close()
    return datos




def db_obtener_movimientos_caja():
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("SELECT fecha, tipo, categoria, descripcion, monto FROM flujo_caja ORDER BY id DESC")
    datos = cursor.fetchall()
    conn.close()
    return datos

def db_insertar_movimiento_caja(tipo, cat, desc, monto):
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO flujo_caja (tipo, categoria, descripcion, monto) VALUES (?, ?, ?, ?)", (tipo, cat, desc, monto))
    conn.commit()
    conn.close()







def db_obtener_tasa_dolar():
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("SELECT valor FROM configuracion WHERE clave = 'tasa_dolar'")
    res = cursor.fetchone()
    conn.close()
    return float(res[0]) if res else 1.0

def db_actualizar_tasa_dolar(nueva_tasa):
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO configuracion (clave, valor) VALUES ('tasa_dolar', ?)", (str(nueva_tasa),))
    conn.commit()
    conn.close()



def db_obtener_detalles_productos_venta(venta_id):
    """
    REEMPLAZA A LA FUNCIÓN VIEJA.
    Vincula los detalles con la tabla de productos usando el código de barras
    para traer el nombre correcto de los artículos facturados en el historial.
    """
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.nombre, dv.cantidad, dv.precio_unitario 
        FROM detalles_ventas dv
        JOIN productos p ON dv.producto_codigo = p.codigo_barras
        WHERE dv.venta_id = ?
    """, (venta_id,))
    datos = cursor.fetchall()
    conn.close()
    return datos


def db_reporte_flotante_ganancias(fecha_filtro):
    """
    Calcula la ganancia bruta y los métodos de pago para un único día específico.
    """
    import sqlite3
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    
    # 1. Consulta de ganancia bruta real cruzando con el precio_compra de productos
    query_ganancia = """
        SELECT 
            SUM(dv.cantidad * dv.precio_unitario) - SUM(dv.cantidad * p.precio_compra)
        FROM detalles_ventas dv
        JOIN ventas v ON dv.venta_id = v.id
        JOIN productos p ON dv.producto_codigo = p.codigo_barras
        WHERE date(v.fecha_hora) = date(?);
    """
    cursor.execute(query_ganancia, (fecha_filtro,))
    res_g = cursor.fetchone()
    ganancia = res_g[0] if res_g and res_g[0] is not None else 0.0

    # 2. Consulta de desgloses de métodos de pago
    query_metodos = """
        SELECT metodo_pago, SUM(total) 
        FROM ventas 
        WHERE date(fecha_hora) = date(?) 
        GROUP BY metodo_pago;
    """
    cursor.execute(query_metodos, (fecha_filtro,))
    res_m = cursor.fetchall()
    conn.close()
    
    # Mapeo seguro en diccionario para CELER
    dicc_metodos = {"EFECTIVO": 0.0, "PUNTO": 0.0, "PAGO MOVIL": 0.0}
    for fila in res_m:
        metodo, monto = fila
        if metodo and metodo.upper() in dicc_metodos:
            dicc_metodos[metodo.upper()] = float(monto) if monto is not None else 0.0
            
    return ganancia, dicc_metodos




# =====================================================================
# 2. PARTE: INTERFACES GRÁFICAS DE LAS PANTALLAS (VISTAS)
# =====================================================================

# --- PANTALLA 1: DASHBOARD CON FILTRO DE FECHAS ---
def vista_dashboard(page: ft.Page):

    

    # Variables de estado locales para almacenar las fechas seleccionadas (Formato YYYY-MM-DD)
    fecha_ini_val = None
    fecha_fin_val = None

    # Contenedores visuales para las métricas (los declaramos arriba para poder actualizarlos)
    txt_v = ft.Text("$0.00", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
    txt_e = ft.Text("$0.00", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
    txt_b = ft.Text("$0.00", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
    txt_a = ft.Text("0", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
    lbl_alertas_msg = ft.Text("", size=14)

    def card_metrica(tit, component_txt, ico, col_ico):
        return ft.Card(
            ft.Container(
                ft.Column([
                    ft.ListTile(
                        leading=ft.Icon(ico, color=col_ico, size=35), 
                        title=ft.Text(tit, size=13, color=ft.Colors.GREY_700), 
                        subtitle=component_txt
                    )
                ]), 
                padding=10, 
                width=220
            ), 
            elevation=2
        )

    # Función interna para refrescar los números en pantalla
    def refrescar_metricas():
        t_ventas, t_egresos, b_neto, p_alerta = db_obtener_resumen(fecha_ini_val, fecha_fin_val)
        
        # Actualizar textos
        txt_v.value = f"${t_ventas:.2f}"
        txt_e.value = f"${t_egresos:.2f}"
        txt_b.value = f"${b_neto:.2f}"
        txt_b.color = ft.Colors.GREEN_700 if b_neto >= 0 else ft.Colors.RED_700
        txt_a.value = str(p_alerta)
        txt_a.color = ft.Colors.RED_700 if p_alerta > 0 else ft.Colors.GREEN_700
        
        lbl_alertas_msg.value = "• Alertas de Stock: " + (f"¡Atención! Tienes {p_alerta} producto(s) en mínimo físico." if p_alerta > 0 else "Inventario saludable.")
        page.update()

    # Botones de texto que muestran la fecha seleccionada
    btn_fecha_ini = ft.TextButton("Seleccionar Inicio", icon=ft.Icons.CALENDAR_MONTH)
    btn_fecha_fin = ft.TextButton("Seleccionar Fin", icon=ft.Icons.CALENDAR_MONTH)

    # Componentes DatePicker (Calendarios)
    def cambiar_fecha_inicio(e):
        nonlocal fecha_ini_val
        if e.control.value:
            fecha_ini_val = e.control.value.strftime("%Y-%m-%d")
            btn_fecha_ini.text = e.control.value.strftime("%d/%m/%Y")
            page.update()

    def cambiar_fecha_fin(e):
        nonlocal fecha_fin_val
        if e.control.value:
            fecha_fin_val = e.control.value.strftime("%Y-%m-%d")
            btn_fecha_fin.text = e.control.value.strftime("%d/%m/%Y")
            page.update()

    picker_inicio = ft.DatePicker(on_change=cambiar_fecha_inicio)
    picker_fin = ft.DatePicker(on_change=cambiar_fecha_fin)
    
    # Agregamos los pickers a las overlays de la página para que puedan abrirse
    page.overlay.extend([picker_inicio, picker_fin])

    def abrir_inicio(e):
        picker_inicio.open = True
    page.update()

    def abrir_fin(e):
        picker_fin.open = True
    page.update()

    btn_fecha_ini.on_click = abrir_inicio
    btn_fecha_fin.on_click = abrir_fin

    # Botones de acción del filtro
    def filtrar_click(e):
        refrescar_metricas()

    def limpiar_filtro_click(e):
        nonlocal fecha_ini_val, fecha_fin_val
        fecha_ini_val = None
        fecha_fin_val = None
        btn_fecha_ini.text = "Seleccionar Inicio"
        btn_fecha_fin.text = "Seleccionar Fin"
        refrescar_metricas()

    btn_filtrar = ft.ElevatedButton("Filtrar", icon=ft.Icons.FILTER_ALT, on_click=filtrar_click, bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE)
    btn_limpiar = ft.IconButton(icon=ft.Icons.REFRESH, on_click=limpiar_filtro_click, tooltip="Mostrar Histórico Total")

    # Carga inicial de datos históricos globales
    refrescar_metricas()



    # Variables de estado internas para el diálogo flotante
    import datetime
    fecha_reporte_val = datetime.date.today().strftime("%Y-%m-%d")

    # Controles visuales del reporte flotante
    lbl_float_titulo = ft.Text(f"Auditoría: {datetime.date.today().strftime('%d/%m/%Y')}", weight=ft.FontWeight.BOLD, size=15)
    lbl_float_ganancia = ft.Text("$0.00 (0.00 Bs.)", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700)
    lbl_float_efectivo = ft.Text("$0.00 (0.00 Bs.)", size=18, weight=ft.FontWeight.W_500)
    lbl_float_punto = ft.Text("$0.00 (0.00 Bs.)", size=18, weight=ft.FontWeight.W_500)
    lbl_float_pago_movil = ft.Text("$0.00 (0.00 Bs.)", size=18, weight=ft.FontWeight.W_500)

    def refrescar_reporte_flotante(fecha_a_consultar):
        try:
            tasa = float(db_obtener_tasa_dolar())
        except Exception:
            tasa = 1.0
            
        # 📍 Le pasamos directamente la fecha que recibe por parámetro
        ganancia, metodos = db_reporte_flotante_ganancias(fecha_a_consultar)
        
        # Asignamos los valores calculados a las etiquetas correspondientes
        lbl_float_ganancia.value = f"${ganancia:.2f} ({ganancia * tasa:.2f} Bs.)"
        lbl_float_efectivo.value = f"${metodos['EFECTIVO']:.2f} ({metodos['EFECTIVO'] * tasa:.2f} Bs.)"
        lbl_float_punto.value = f"${metodos['PUNTO']:.2f} ({metodos['PUNTO'] * tasa:.2f} Bs.)"
        lbl_float_pago_movil.value = f"${metodos['PAGO MOVIL']:.2f} ({metodos['PAGO MOVIL'] * tasa:.2f} Bs.)"
        page.update()

    def al_cambiar_fecha_reporte(e):
        nonlocal fecha_reporte_val
        if e.control.value:
            # 📍 Guardamos la fecha seleccionada en formato correcto para la BD
            fecha_reporte_val = e.control.value.strftime("%Y-%m-%d")
            lbl_float_titulo.value = f"Auditoría: {e.control.value.strftime('%d/%m/%Y')}"
            lbl_float_titulo.update()
            
            # 📍 Forzamos la consulta pasando directamente la nueva fecha elegida
            refrescar_reporte_flotante(fecha_reporte_val)

    picker_reporte = ft.DatePicker(on_change=al_cambiar_fecha_reporte)
    page.overlay.append(picker_reporte)

    # Ventana de diálogo flotante estructurada (Se mantiene intacta)
    dialogo_reporte = ft.AlertDialog(
        title=ft.Row([ft.Icon(ft.Icons.ANALYTICS_ROUNDED, color=ft.Colors.BLUE_800), ft.Text("Reporte Diario de Utilidades", size=16, weight=ft.FontWeight.BOLD)]),
        content=ft.Column([
            ft.Row([lbl_float_titulo, ft.IconButton(icon=ft.Icons.CALENDAR_MONTH, on_click=lambda _: page.show_dialog(picker_reporte) if hasattr(page, "show_dialog") else page.open(picker_reporte))]),
            ft.Divider(),
            ft.Text("GANANCIA BRUTA REAL:", size=16, color="grey", weight="bold"),
            lbl_float_ganancia,
            ft.Container(height=10),
            ft.Text("DESGLOSE POR MÉTODOS:", size=14, color="grey", weight="bold"),
            ft.ListTile(leading=ft.Icon(ft.Icons.MONEY, color="green"), title=ft.Text("Efectivo"), trailing=lbl_float_efectivo, dense=True),
            ft.ListTile(leading=ft.Icon(ft.Icons.CREDIT_CARD, color="blue"), title=ft.Text("Punto"), trailing=lbl_float_punto, dense=True),
            ft.ListTile(leading=ft.Icon(ft.Icons.PHONELINK_RING, color="purple"), title=ft.Text("Pago Móvil"), trailing=lbl_float_pago_movil, dense=True),
        ], tight=True, width=320),
        actions=[ft.TextButton("Cerrar", on_click=lambda e: setattr(dialogo_reporte, "open", False) or page.update())],
        actions_alignment=ft.MainAxisAlignment.END
    )
    page.overlay.append(dialogo_reporte)

    def abrir_modal_reporte(e):
        import datetime
        nonlocal fecha_reporte_val
        # Seteamos por defecto el día de hoy al abrir
        fecha_reporte_val = datetime.date.today().strftime("%Y-%m-%d")
        lbl_float_titulo.value = f"Auditoría: {datetime.date.today().strftime('%d/%m/%Y')}"
        
        dialogo_reporte.open = True
        page.update()
        
        # 📍 Forzamos la consulta inicial pasándole la fecha de hoy
        refrescar_reporte_flotante(fecha_reporte_val)

    # Botón disparador del reporte
    btn_abrir_reporte = ft.ElevatedButton(
        "📊 Ver Utilidad y Cierre", 
        icon=ft.Icons.INSERT_CHART_OUTLINED_ROUNDED, 
        on_click=abrir_modal_reporte, 
        bgcolor=ft.Colors.GREEN_700, 
        color=ft.Colors.WHITE
    )






    return ft.Column([
        ft.Text("📊 Tablero de Control / Finanzas", size=26, weight=ft.FontWeight.BOLD),
        ft.Text("Resumen analítico de las finanzas. Filtra por rango para auditar periodos específicos.", size=15, color=ft.Colors.GREY_700),
        ft.Divider(),
        
        # Nueva Fila de Filtros de Fecha
# --- REEMPLAZA TU CONTENEDOR DE FILTROS POR ESTE ---
        # Busca este contenedor exacto en tu return original y déjalo configurado así:
        ft.Container(
            content=ft.Row([
                ft.Text("Rango:", weight=ft.FontWeight.BOLD, size=14),
                btn_fecha_ini,
                ft.Text("al"),
                btn_fecha_fin,
                btn_filtrar,
                btn_limpiar,
                # ➕ AGREGAMOS EL NUEVO BOTÓN FLOTANTE AL FINAL DE LA FILA:
                btn_abrir_reporte 
            ], 
            alignment=ft.MainAxisAlignment.START, 
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,       
            run_spacing=10   
            ),
            padding=10,
            bgcolor=ft.Colors.GREY_100,
            border_radius=8
        ),

        ft.Container(height=10),
        
        # Fila de Tarjetas de Métricas
        ft.Row([
            card_metrica("Total Ventas", txt_v, ft.Icons.ATTACH_MONEY, ft.Colors.GREEN),
            card_metrica("Total Egresos", txt_e, ft.Icons.MONEY_OFF, ft.Colors.RED_700),
            card_metrica("Balance Neto", txt_b, ft.Icons.ACCOUNT_BALANCE_WALLET, ft.Colors.BLUE),
            card_metrica("Productos Alerta", txt_a, ft.Icons.WARNING_AMBER, ft.Colors.ORANGE)
        ], wrap=True, spacing=15),
        
 ft.Container(height=20),
        
        ft.Container(
            content=ft.Column([
                ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.BLUE_ACCENT), ft.Text("Estado de Alertas:", weight=ft.FontWeight.BOLD)]),
                lbl_alertas_msg
            ]), 
            padding=15, 
            bgcolor=ft.Colors.GREY_50, 
            border_radius=8, 
            border=ft.Border.all(1, ft.Colors.GREY_300)
        )
    ], expand=True, scroll=ft.ScrollMode.ALWAYS) # <-- Este cierre ahora le pertenece a la Column principal de la vista

# --- PANTALLA 2: INVENTARIO COMPLETO UNIFICADO Y FILTRABLE (PARTE 1 CORREGIDA) ---
def vista_inventario(page: ft.Page):
    page.floating_action_button = None
    page.update()

    # DIÁLOGO GLOBAL PERSISTENTE PARA CONFIRMACIONES EN FLET 0.85/0.86
    def cerrar_dialogo_inv(e=None):
        dialogo_comun_inv.open = False
        page.update()

    dialogo_comun_inv = ft.AlertDialog(
        modal=True,
        title=ft.Text("Aviso del Sistema"),
        content=ft.Text("Procesando información..."),
        actions=[
            ft.TextButton("Ok", on_click=cerrar_dialogo_inv)
        ],
        actions_alignment=ft.MainAxisAlignment.END
    )
    page.overlay.append(dialogo_comun_inv)

    # Variable de estado que ahora guarda el código de barras del producto que se está editando
    producto_codigo_editar = None

    # Controles del formulario principal
    txt_codigo_barras = ft.TextField(label="Código de Barras", dense=True, hint_text="Escanee o ingrese un código único")
    txt_nombre = ft.TextField(label="Producto", dense=True)
    txt_p_compra = ft.TextField(label="Costo ($)", dense=True, hint_text="Ej: 10.50") 
    txt_p_venta = ft.TextField(label="Venta ($)", dense=True, hint_text="Ej: 15.00")  
    txt_stock = ft.TextField(label="Stock", value="0", keyboard_type=ft.KeyboardType.NUMBER, dense=True)
    txt_stock_min = ft.TextField(label="Mín.", value="5", keyboard_type=ft.KeyboardType.NUMBER, dense=True)

    # Controles dinámicos específicos para la modalidad de CAJAS
    txt_contenido_caja = ft.TextField(label="Contenido por Caja", value="1", keyboard_type=ft.KeyboardType.NUMBER, dense=True)
    txt_precio_caja = ft.TextField(label="Precio por Caja ($)", value="0.00", dense=True)

    # Contenedor visual que oculta o muestra los campos de la caja de forma dinámica
    panel_caja = ft.Row([
        ft.Container(txt_contenido_caja, expand=True),
        ft.Container(txt_precio_caja, expand=True)
    ], visible=False, spacing=10)

    # Dropdown reactivo para seleccionar la unidad de medida del producto
    def al_cambiar_unidad_medida(e):
        # El panel de cajas se vuelve visible ÚNICAMENTE si seleccionan la opción CAJA
        panel_caja.visible = (dd_unidad.value == "CAJA")
        page.update()

    dd_unidad = ft.Dropdown(
        label="Medida",
        options=[
            ft.dropdown.Option("UNIDAD"),
            ft.dropdown.Option("KG"),
            ft.dropdown.Option("CAJA")
        ],
        value="UNIDAD",
        dense=True
    )
    dd_unidad.on_change = al_cambiar_unidad_medida

    lista_productos_movil = ft.ListView(expand=True, spacing=5)

    def limpiar_campos():
        nonlocal producto_codigo_editar
        producto_codigo_editar = None
        txt_codigo_barras.disabled = False  # Reactivamos el campo de código para nuevas inserciones
        txt_codigo_barras.value = ""
        txt_nombre.value = ""
        txt_p_compra.value = ""
        txt_p_venta.value = ""
        txt_stock.value = "0"
        txt_stock_min.value = "5"
        dd_unidad.value = "UNIDAD"
        txt_contenido_caja.value = "1"
        txt_precio_caja.value = "0.00"
        panel_caja.visible = False
        
        btn_guardar.content = ft.Text("Añadir")
        btn_guardar.icon = ft.Icons.ADD
        btn_guardar.bgcolor = ft.Colors.GREEN
        btn_cancelar.visible = False
        page.update()

        forzar_foco_inventario(txt_codigo_barras)

    def al_buscar_inventario(e):
        renderizar_tarjetas_filtradas()

    txt_buscar_inv = ft.TextField(
        label="Buscar producto en almacén...",
        prefix_icon=ft.Icons.SEARCH,
        dense=True,
        on_change=al_buscar_inventario
    )

    def renderizar_tarjetas_filtradas():
        lista_productos_movil.controls.clear()
        tasa = float(db_obtener_tasa_dolar())
        texto_busqueda = txt_buscar_inv.value.lower().strip() if txt_buscar_inv.value else ""
        
        lista_productos_db = txt_buscar_inv.data if txt_buscar_inv.data else []
        
        for prod in lista_productos_db:
            # Desempaquetamos de forma exacta los 9 valores devueltos por el nuevo backend unificado
            c_barras, nombre, p_compra, p_venta, stock, stock_min, u_medida, cont_caja, p_caja = prod
            
            if texto_busqueda and texto_busqueda not in nombre.lower() and texto_busqueda not in str(c_barras).lower():
                continue
                
            # Evaluamos las alertas soportando los stocks de tipo decimal (Kg)
            en_alerta = float(stock) <= float(stock_min)
            precio_dolar = float(p_venta)
            p_venta_bs = precio_dolar * tasa
            col_stock = ft.Colors.RED_700 if en_alerta else ft.Colors.GREEN_700
            
            # Formateamos el indicativo de stock visual en la tarjeta
            stock_visual = f"{stock:.3f} Kg" if u_medida == "KG" else f"{int(stock)} un"
            
            # Variables de contexto actual para la acción de los botones de edición
            cb_actual = str(c_barras)
            n_actual = nombre
            pc_actual = float(p_compra)
            pv_actual = precio_dolar
            st_actual = float(stock)
            sm_actual = float(stock_min)
            um_actual = str(u_medida)
            cc_actual = int(cont_caja)
            prc_actual = float(p_caja)

            lista_productos_movil.controls.append(
                ft.Card(
                    content=ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.SCALE if u_medida == "KG" else ft.Icons.INVENTORY_2, color=col_stock, size=24),
                            ft.Column([
                                ft.Text(str(nombre), weight=ft.FontWeight.BOLD, size=14),
                                ft.Text(f"Ref: {cb_actual} | Costo: ${pc_actual:.2f} | Mín: {sm_actual}", size=14, color=ft.Colors.GREY_600)
                            ], spacing=2, expand=True),
                            ft.Column([
                                ft.Text(f"${pv_actual:.2f}", weight=ft.FontWeight.BOLD, size=15, color=ft.Colors.BLUE_900),
                                ft.Text(f"{p_venta_bs:.1f} Bs.", size=14, color=ft.Colors.GREY_700)
                            ], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=2),
                            ft.Row([
                                ft.IconButton(
                                    icon=ft.Icons.EDIT,
                                    icon_color=ft.Colors.BLUE_500,
                                    icon_size=18,
                                    on_click=lambda e, cb=cb_actual, n=n_actual, pc=pc_actual, pv=pv_actual, s=st_actual, sm=sm_actual, um=um_actual, cc=cc_actual, prc=prc_actual: preparar_edicion(cb, n, pc, pv, s, sm, um, cc, prc)
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.DELETE,
                                    icon_color=ft.Colors.RED_500,
                                    icon_size=18,
                                    on_click=lambda e, cb=cb_actual, n=n_actual: abrir_confirmacion_borrado(cb, n)
                                )
                            ], spacing=0, alignment=ft.MainAxisAlignment.END),
                            ft.Container(
                                content=ft.Text(stock_visual, color=ft.Colors.WHITE, size=14, weight=ft.FontWeight.BOLD),
                                bgcolor=col_stock,
                                padding=5, 
                                border_radius=12
                            )
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        padding=10
                    ),
                    elevation=1
                )
            )
        page.update()

# --- PANTALLA 2: INVENTARIO COMPLETO UNIFICADO Y FILTRABLE (PARTE 2 CORREGIDA) ---
# Pega esto exactamente debajo de la primera parte, conservando la indentación de la vista principal.

    def actualizar():
        try:
            txt_buscar_inv.data = db_obtener_productos()
        except Exception:
            txt_buscar_inv.data = []
            
        renderizar_tarjetas_filtradas()

    def preparar_edicion(codigo_barras, nombre, p_compra, p_venta, stock, stock_min, unidad_medida, contenido_caja, precio_caja):
        nonlocal producto_codigo_editar
        producto_codigo_editar = codigo_barras
        
        txt_codigo_barras.value = str(codigo_barras)
        txt_codigo_barras.disabled = True  # Bloqueamos el ID clave para evitar alteraciones en caliente
        
        txt_nombre.value = str(nombre)
        txt_p_compra.value = str(p_compra)
        txt_p_venta.value = str(p_venta)
        txt_stock.value = str(stock)
        txt_stock_min.value = str(stock_min)
        dd_unidad.value = str(unidad_medida)
        txt_contenido_caja.value = str(contenido_caja)
        txt_precio_caja.value = str(precio_caja)
        
        # Sincronizamos la visibilidad del panel inferior de cajas automáticamente
        panel_caja.visible = (unidad_medida == "CAJA")
        
        btn_guardar.content = ft.Text("Actualizar")
        btn_guardar.icon = ft.Icons.SAVE
        btn_guardar.bgcolor = ft.Colors.BLUE_800
        btn_cancelar.visible = True
        page.update()

    def abrir_confirmacion_borrado(codigo_barras, nombre_p):
        def confirmar(e):
            db_eliminar_producto(codigo_barras)
            cerrar_dialogo_inv()
            actualizar()

        dialogo_comun_inv.title = ft.Text("Confirmar Eliminación")
        dialogo_comun_inv.content = ft.Text(f"¿De verdad deseas borrar '{nombre_p}' del inventario?")
        dialogo_comun_inv.actions = [
            ft.TextButton("Cancelar", on_click=cerrar_dialogo_inv),
            ft.ElevatedButton(content=ft.Text("Eliminar"), bgcolor=ft.Colors.RED, color=ft.Colors.WHITE, on_click=confirmar),
        ]
        dialogo_comun_inv.actions_alignment = ft.MainAxisAlignment.END
        dialogo_comun_inv.open = True
        page.update()

    def guardar_click(e):
        if not all([txt_codigo_barras.value, txt_nombre.value, txt_p_compra.value, txt_p_venta.value, txt_stock.value, txt_stock_min.value]): 
            return
        try:
            cod_b = txt_codigo_barras.value.strip()
            nom = txt_nombre.value.strip()
            costo_texto = txt_p_compra.value.strip().replace(",", ".")
            venta_texto = txt_p_venta.value.strip().replace(",", ".")
            pc = float(costo_texto)
            pv = float(venta_texto)
            
            # Leemos la cantidad que el usuario digitó en la caja de texto
            cantidad_ingresada = float(txt_stock.value.strip()) if txt_stock.value.strip() else 0.0
            sm = float(txt_stock_min.value.strip())
            
            u_medida = dd_unidad.value
            cont_c = int(txt_contenido_caja.value.strip()) if u_medida == "CAJA" else 1
            pr_caja = float(txt_precio_caja.value.strip().replace(",", ".")) if u_medida == "CAJA" else 0.0

            if producto_codigo_editar is not None:
                # --- ¡AQUÍ ESTÁ EL CAMBIO DE SUMA! ---
                # Buscamos cuánto stock tenía previamente guardado este producto en el catálogo en caché
                stock_previo = 0.0
                lista_productos_db = txt_buscar_inv.data if txt_buscar_inv.data else []
                for prod in lista_productos_db:
                    if str(prod[0]).lower() == cod_b.lower():
                        stock_previo = float(prod[4])  # prod[4] es la posición del stock actual
                        break
                
                # Si entramos por el escáner (el campo comenzó vacío), SUMAMOS la nueva mercancía.
                # Si entramos dándole clic al botón azul de EDITAR de la tarjeta (el campo traía el número completo),
                # evaluamos si el valor cambió o si queremos mantener el comportamiento de edición directa.
                # Para asegurar que la carga sume siempre, consolidamos el nuevo stock total:
                st = stock_previo + cantidad_ingresada
                
                db_actualizar_producto(cod_b, nom, pc, pv, st, sm, u_medida, cont_c, pr_caja)
            else:
                # Si es un registro totalmente nuevo, el stock total es simplemente lo ingresado
                st = cantidad_ingresada
                db_insertar_producto(cod_b, nom, pc, pv, st, sm, u_medida, cont_c, pr_caja)

            limpiar_campos()
            actualizar()
        except ValueError: 
            pass

    def forzar_foco_inventario(control_destino):
        async def ejecutar_foco_asincrono():
            await control_destino.focus()
            page.update()
        page.run_task(ejecutar_foco_asincrono)



    def al_escanear_codigo_inventario(e):
        codigo_ingresado = txt_codigo_barras.value.strip()
        if not codigo_ingresado:
            return

        lista_productos_db = txt_buscar_inv.data if txt_buscar_inv.data else []
        
        producto_encontrado = None
        for prod in lista_productos_db:
            c_barras, nombre, p_compra, p_venta, stock, stock_min, u_medida, cont_caja, p_caja = prod
            if str(c_barras).lower() == codigo_ingresado.lower():
                producto_encontrado = prod
                break

        if producto_encontrado:
            c_barras, nombre, p_compra, p_venta, stock, stock_min, u_medida, cont_caja, p_caja = producto_encontrado
            
            nonlocal producto_codigo_editar
            producto_codigo_editar = c_barras
            txt_codigo_barras.disabled = True  

            txt_nombre.value = str(nombre)
            txt_p_compra.value = str(p_compra)
            txt_p_venta.value = str(p_venta)
            txt_stock.value = "" # Queda limpio para recibir la nueva mercancía
            txt_stock_min.value = str(stock_min)
            dd_unidad.value = str(u_medida)
            txt_contenido_caja.value = str(cont_caja)
            txt_precio_caja.value = str(p_caja)
            
            panel_caja.visible = (u_medida == "CAJA")
            btn_guardar.content = ft.Text("Actualizar")
            btn_guardar.icon = ft.Icons.SAVE
            btn_guardar.bgcolor = ft.Colors.BLUE_800
            btn_cancelar.visible = True
            
            # 📍 ACTUALIZAMOS LA INTERFAZ PRIMERO PARA ASENTAR LOS TEXTOS
            page.update()
            # 📍 Y AHORA SÍ LE CLAVAMOS EL FOCO DE FORMA ABSOLUTA
            forzar_foco_inventario(txt_stock)
        else:
            txt_nombre.value = ""
            txt_p_compra.value = ""
            txt_p_venta.value = ""
            txt_stock.value = "0"
            txt_stock_min.value = "5"
            dd_unidad.value = "UNIDAD"
            txt_contenido_caja.value = "1"
            txt_precio_caja.value = "0.00"
            panel_caja.visible = False
            
            producto_codigo_editar = None
            txt_codigo_barras.disabled = False
            btn_guardar.content = ft.Text("Añadir")
            btn_guardar.icon = ft.Icons.ADD
            btn_guardar.bgcolor = ft.Colors.GREEN
            
            # 📍 REPETIMOS EL PROCESO: ACTUALIZACIÓN SEGUIDA DE FOCO
            page.update()
            forzar_foco_inventario(txt_nombre)
            
        page.update()

    # =========================================================================
    # DEFINICIÓN DE CONTROLES DE ACCIÓN (Tu Bloque 7)
    # =========================================================================
    btn_guardar = ft.ElevatedButton(
        content=ft.Text("Añadir"), 
        icon=ft.Icons.ADD, 
        on_click=guardar_click, 
        bgcolor=ft.Colors.GREEN, 
        color=ft.Colors.WHITE, 
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))
    )
    btn_cancelar = ft.TextButton("Cancelar", on_click=lambda e: limpiar_campos(), visible=False)

    # Recuerda asignarle a tu "txt_codigo_barras" del Bloque 1 la propiedad: 
    # on_submit=al_escanear_codigo_inventario
    txt_codigo_barras.on_submit = al_escanear_codigo_inventario

    actualizar()


    forzar_foco_inventario(txt_codigo_barras)

    return ft.Column([
        ft.Text("📦 Inventario General", size=20, weight=ft.FontWeight.BOLD),
        ft.ResponsiveRow([
            ft.Container(txt_codigo_barras, col=4),
            ft.Container(txt_nombre, col=5),
            ft.Container(dd_unidad, col=3),
            
            ft.Container(txt_p_compra, col=3),
            ft.Container(txt_p_venta, col=3),
            ft.Container(txt_stock, col=3),
            ft.Container(txt_stock_min, col=3),
        ], run_spacing=5),
        
        panel_caja,
        
        ft.Row([btn_cancelar, btn_guardar], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, spacing=10),
        
        txt_buscar_inv,
        
        ft.Divider(height=10),
        lista_productos_movil
    ], expand=True)






    btn_guardar = ft.ElevatedButton(
        content=ft.Text("Añadir"), 
        icon=ft.Icons.ADD, 
        on_click=guardar_click, 
        bgcolor=ft.Colors.GREEN, 
        color=ft.Colors.WHITE, 
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))
    )
    btn_cancelar = ft.TextButton("Cancelar", on_click=lambda e: limpiar_campos(), visible=False)

    actualizar()

    return ft.Column([
        ft.Text("📦 Inventario General", size=20, weight=ft.FontWeight.BOLD),
        ft.ResponsiveRow([
            ft.Container(txt_codigo_barras, col=4),
            ft.Container(txt_nombre, col=5),
            ft.Container(dd_unidad, col=3),
            
            ft.Container(txt_p_compra, col=3),
            ft.Container(txt_p_venta, col=3),
            ft.Container(txt_stock, col=3),
            ft.Container(txt_stock_min, col=3),
        ], run_spacing=5),
        
        # Panel que renderiza dinámicamente el contenido y costo de cajas
        panel_caja,
        
        ft.Row([btn_cancelar, btn_guardar], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, spacing=10),
        
        txt_buscar_inv,
        
        ft.Divider(height=10),
        lista_productos_movil
    ], expand=True)




# --- PANTALLA 3: PUNTO DE VENTA HÍBRIDO (BLOQUE 1: INICIO Y DIÁLOGOS OVERLAY) ---
import os
import datetime
import flet as ft

class ElementoCarrito:
    def __init__(self, id_p, nombre, precio, cantidad, stock_max, unidad_medida="UNIDAD", es_caja=0, contenido_caja=1):
        self.id = str(id_p)
        self.nombre = str(nombre)
        self.precio = float(precio)
        self.cantidad = float(cantidad)
        self.stock_max = float(stock_max)
        self.unidad_medida = str(unidad_medida)
        self.es_caja = int(es_caja)
        self.contenido_caja = int(contenido_caja)

carrito_chicha = []
productos_en_memoria = []

def vista_ventas(page: ft.Page):
    page.floating_action_button = None
    page.update()

    # --- DIÁLOGOS GLOBALES SEGUROS INSTALADOS EN EL OVERLAY (FLET 0.85.5) ---
    txt_referencia = ft.TextField(label="Número de Referencia", autofocus=True, dense=True, keyboard_type=ft.KeyboardType.NUMBER)
    cerrar_dialogo_referencia = lambda e=None: setattr(dialogo_referencia, "open", False) or page.update()

    dialogo_referencia = ft.AlertDialog(
        modal=True,
        title=ft.Row([ft.Icon(ft.Icons.PHONELINK_RING_ROUNDED, color=ft.Colors.BLUE_ACCENT_700), ft.Text("Confirmar Pago Móvil", size=16, weight=ft.FontWeight.BOLD)], spacing=10),
        content=ft.Column([ft.Text("Por favor, ingrese el número de referencia de la transacción:"), txt_referencia], tight=True, width=300),
        actions=[ft.TextButton("Cancelar", on_click=cerrar_dialogo_referencia)],
        actions_alignment=ft.MainAxisAlignment.END
    )
    page.overlay.append(dialogo_referencia)

    txt_peso_entrada = ft.TextField(label="Cantidad a vender", autofocus=True, dense=True, keyboard_type=ft.KeyboardType.NUMBER)
    cerrar_dialogo_peso = lambda e=None: setattr(dialogo_peso, "open", False) or page.update()
        
    dialogo_peso = ft.AlertDialog(
        modal=True,
        title=ft.Text("Ingresar Cantidad"),
        content=txt_peso_entrada,
        actions=[ft.TextButton("Cancelar", on_click=cerrar_dialogo_peso)],
        actions_alignment=ft.MainAxisAlignment.END
    )
    page.overlay.append(dialogo_peso)

    # --- CONTROLES DE LA INTERFAZ ORIGINAL ---
    cuadricula_productos = ft.GridView(expand=False, height=180, runs_count=3, max_extent=160, spacing=8, run_spacing=8, scroll=ft.ScrollMode.ALWAYS)
    lista_productos_ui = ft.ListView(expand=True, height=150, spacing=5, scroll=ft.ScrollMode.ALWAYS)
    lista_carrito = ft.ListView(height=220, spacing=0, scroll=ft.ScrollMode.ALWAYS)
    lbl_total = ft.Text("Total: $0.00 (0.00 Bs.)", size=19, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700)
    
    dd_metodo = ft.Dropdown(
        label="Pago", 
        options=[ft.dropdown.Option("EFECTIVO"), ft.dropdown.Option("PUNTO"), ft.dropdown.Option("PAGO MOVIL")], 
        value="EFECTIVO", dense=True, width=150 
    )
    

# --- BLOQUE 2: LÓGICA INTERNA Y RENDERIZADO DEL CARRITO ---

    def render_carrito():
        lista_carrito.controls.clear()
        total_acumulado = 0.0
        try:
            tasa = float(db_obtener_tasa_dolar())
        except NameError:
            tasa = 1.0
        
        for idx, art in enumerate(carrito_chicha):
            subtotal_renglon = art.precio * art.cantidad
            total_acumulado += subtotal_renglon
            id_actual = idx
            
            sufijo = " Kg" if art.unidad_medida == "KG" else (" Cajas" if art.es_caja == 1 else " Unid")
            cant_str = f"{art.cantidad:.3f}" if art.unidad_medida == "KG" else f"{int(art.cantidad)}"

            lista_carrito.controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Text(f"{cant_str}{sufijo}", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800, size=19),
                        ft.Text(art.nombre, expand=True, size=19), 
                        ft.Text(f"${subtotal_renglon:.2f}", size=19, weight=ft.FontWeight.BOLD),
                        ft.Row([
                            ft.IconButton(icon=ft.Icons.REMOVE_CIRCLE_OUTLINE, icon_color=ft.Colors.AMBER_700, icon_size=19, padding=0, on_click=lambda _, i=id_actual: restar_unidad(i)),
                            ft.IconButton(icon=ft.Icons.ADD_CIRCLE_OUTLINE, icon_color=ft.Colors.GREEN_600, icon_size=19, padding=0, on_click=lambda _, objeto_art=art: agregar_al_carrito_objeto(objeto_art)),
                            ft.IconButton(icon=ft.Icons.DELETE_OUTLINE_ROUNDED, icon_color=ft.Colors.RED_500, icon_size=19, padding=0, on_click=lambda _, i=id_actual: quitar(i))
                        ], spacing=4)
                    ], spacing=5, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    padding=4, margin=0, height=32
                )
            )
        lbl_total.value = f"Total: ${total_acumulado:.2f} ({total_acumulado * tasa:.2f} Bs.)"
        page.update()

    # === REEMPLAZA ÚNICAMENTE ESTA FUNCIÓN EN TU CÓDIGO ACTUAL ===
    def forzar_foco_absoluto():
        async def ejecutar_foco_asincrono():
            await txt_buscar.focus() # <-- ¡AQUÍ ESTÁ EL AWAIT QUE EXIGÍA LA CONSOLA!
            page.update()
        page.run_task(ejecutar_foco_asincrono)

    def quitar(index):
        carrito_chicha.pop(index)
        render_carrito()
        forzar_foco_absoluto()

    def restar_unidad(index):
        art = carrito_chicha[index]
        if art.unidad_medida == "KG":
            if art.cantidad > 0.101: 
                art.cantidad -= 0.1
                art.whitespace_qty = art.cantidad
            else: 
                carrito_chicha.pop(index)
        else:
            if art.cantidad > 1: 
                art.cantidad -= 1
            else: 
                carrito_chicha.pop(index)
        render_carrito()
        forzar_foco_absoluto()

    def agregar_al_carrito_objeto(objeto_art):
        incremento = 0.1 if objeto_art.unidad_medida == "KG" else 1
        if (objeto_art.cantidad + incremento) <= objeto_art.stock_max:
            objeto_art.cantidad += incremento
            objeto_art.whitespace_qty = objeto_art.cantidad
            render_carrito()
        else:
            page.snack_bar = ft.SnackBar(ft.Text("No hay suficiente stock"), bgcolor="orange")
            page.snack_bar.open = True
        forzar_foco_absoluto()

    # --- BLOQUE 3: AGREGAR PRODUCTOS AL CARRITO Y DIÁLOGOS DINÁMICOS ---

    def agregar_al_carrito(prod):
        p_id, p_nombre, p_venta, p_stock, u_medida, cont_caja, p_caja = prod
        p_stock = float(p_stock)

        if u_medida == "KG":
            txt_peso_entrada.value = ""
            txt_peso_entrada.error_text = None
            
            def guardar_peso(ev):
                try: 
                    peso_vender = float(txt_peso_entrada.value.strip())
                except ValueError: 
                    return
                if peso_vender <= 0 or peso_vender > p_stock: 
                    return
                
                cerrar_dialogo_peso()
                # Si el producto por KG ya está en el carrito, se actualiza y se mueve al inicio (arriba)
                for idx, art in enumerate(carrito_chicha):
                    if art.id == str(p_id):
                        art.cantidad += peso_vender
                        art.whitespace_qty = art.cantidad
                        producto_existente = carrito_chicha.pop(idx)
                        carrito_chicha.insert(0, producto_existente)
                        render_carrito()
                        forzar_foco_absoluto()
                        return
                
                # Si es un producto por KG nuevo en la venta, se inserta en la posición 0
                carrito_chicha.insert(0, ElementoCarrito(p_id, p_nombre, p_venta, peso_vender, p_stock, "KG"))
                render_carrito()
                forzar_foco_absoluto()

            dialogo_peso.title = ft.Text(f"Peso para {p_nombre}")
            dialogo_peso.content = ft.Column([ft.Text(f"Disponible: {p_stock:.3f} Kg"), txt_peso_entrada], tight=True, width=300)
            dialogo_peso.actions = [
                ft.TextButton("Cancelar", on_click=lambda e: cerrar_dialogo_peso() or forzar_foco_absoluto()), 
                ft.ElevatedButton("Agregar", on_click=guardar_peso)
            ]
            dialogo_peso.open = True
            page.update()

        elif u_medida == "CAJA":
            def comprar_como_caja(ev):
                cerrar_dialogo_peso()
                # Se inserta en la posición 0 (arriba)
                carrito_chicha.insert(0, ElementoCarrito(p_id, f"{p_nombre} (CAJA)", p_caja, 1, p_stock, "UNIDAD", 1, cont_caja))
                render_carrito()
                forzar_foco_absoluto()

            def comprar_como_detalle(ev):
                cerrar_dialogo_peso()
                # Se inserta en la posición 0 (arriba)
                carrito_chicha.insert(0, ElementoCarrito(p_id, p_nombre, p_venta, 1, p_stock, "UNIDAD", 0, 1))
                render_carrito()
                forzar_foco_absoluto()

            dialogo_peso.title = ft.Text("Seleccione Presentación")
            dialogo_peso.content = ft.Text(f"¿Cómo desea vender {p_nombre}?")
            dialogo_peso.actions = [
                ft.ElevatedButton("Detalle", on_click=comprar_como_detalle), 
                ft.ElevatedButton("Caja", on_click=comprar_como_caja)
            ]
            dialogo_peso.open = True
            page.update()
            
        else:
            # Si el producto estándar ya existe, incrementa en 1 y salta al tope del carrito (arriba)
            for idx, art in enumerate(carrito_chicha):
                if art.id == str(p_id):
                    art.cantidad += 1
                    art.whitespace_qty = art.cantidad
                    producto_existente = carrito_chicha.pop(idx)
                    carrito_chicha.insert(0, producto_existente)
                    render_carrito()
                    forzar_foco_absoluto()
                    return
            
            # Si el producto estándar es nuevo en la venta, se inserta en la posición 0
            carrito_chicha.insert(0, ElementoCarrito(p_id, p_nombre, p_venta, 1, p_stock, "UNIDAD", 0, 1))
            render_carrito()
            forzar_foco_absoluto()


    # --- BLOQUE 4: FILTRADO, BÚSQUEDA Y RENDERIZADO DEL CATÁLOGO ---

    def filtrar_y_renderizar_catalogo():
        texto_busqueda = txt_buscar.value.lower().strip() if txt_buscar.value else ""
        cuadricula_productos.controls.clear()
        lista_productos_ui.controls.clear()

        # LEEMOS EL MODO DE VISTA EN TIEMPO REAL DESDE EL ARCHIVO
        ruta_txt_config = "config_vista.txt"
        modo_visual = "cuadricula"
        if os.path.exists(ruta_txt_config):
            try:
                with open(ruta_txt_config, "r") as f:
                    modo_visual = f.read().strip().lower()
            except Exception:
                modo_visual = "cuadricula"

        # OCULTAMOS O MOSTRAMOS LOS CONTENEDORES SEGÚN EL MODO
        if modo_visual == "lista":
            cuadricula_productos.visible = False
            lista_productos_ui.visible = True
        else:
            cuadricula_productos.visible = True
            lista_productos_ui.visible = False

        for fila_p in productos_en_memoria:
            cod_barras, nombre, p_compra, p_venta, stock, stock_min, u_medida, cont_caja, p_caja = fila_p
            if texto_busqueda and texto_busqueda not in nombre.lower(): 
                continue

            prod_data = (cod_barras, nombre, p_venta, stock, u_medida, cont_caja, p_caja)
            stock_visual = f"{stock:.3f} Kg" if u_medida == "KG" else f"{int(stock)} un"

            # SI ESTÁ EN MODO CUADRÍCULA, ALIMENTAMOS LA GRID
            if modo_visual == "cuadricula":
                cuadricula_productos.controls.append(
                    ft.Card(content=ft.Container(
                        on_click=lambda e, pd=prod_data: agregar_al_carrito(pd),
                        padding=6, content=ft.Column([
                            ft.Icon(ft.Icons.SCALE if u_medida == "KG" else ft.Icons.LOCAL_DRINK, color=ft.Colors.BLUE_500, size=24),
                            ft.Text(nombre, weight=ft.FontWeight.BOLD, size=12),
                            ft.Text(f"${float(p_venta):.2f}", size=11, color=ft.Colors.GREEN_700),
                            ft.Text(f"Stock: {stock_visual}", size=10)
                        ], alignment=ft.MainAxisAlignment.CENTER, spacing=1)
                    ))
                )
            
            # SI ESTÁ EN MODO LISTA, ALIMENTAMOS EL LISTVIEW
            else:
                lista_productos_ui.controls.append(
                    ft.Card(
                        content=ft.ListTile(
                            leading=ft.Icon(ft.Icons.SCALE if u_medida == "KG" else ft.Icons.LOCAL_DRINK, color=ft.Colors.BLUE_500),
                            title=ft.Text(nombre, weight=ft.FontWeight.BOLD, size=16),
                            subtitle=ft.Text(f"Disponible: {stock_visual}", size=16),
                            trailing=ft.Text(f"${float(p_venta):.2f}", color=ft.Colors.GREEN_700, weight=ft.FontWeight.BOLD, size=16),
                            on_click=lambda e, pd=prod_data: agregar_al_carrito(pd)
                        ),
                        elevation=1
                    )
                )
        page.update()

    def al_buscar_o_escanear_producto(e):
        texto = txt_buscar.value.strip()
        if not texto:
            filtrar_y_renderizar_catalogo()
            return
        for fila_p in productos_en_memoria:
            cod_barras, nombre, p_compra, p_venta, stock, stock_min, u_medida, cont_caja, p_caja = fila_p
            if str(cod_barras) == texto:
                agregar_al_carrito((cod_barras, nombre, p_venta, stock, u_medida, cont_caja, p_caja))
                txt_buscar.value = ""
                txt_buscar.update()
                return
        filtrar_y_renderizar_catalogo()

    # === ASÍ DEBERÍA QUEDAR LA DEFINICIÓN EN TU CÓDIGO ORIGINAL ===
    txt_buscar = ft.TextField(
        label="Escanear o buscar...", 
        prefix_icon=ft.Icons.BARCODE_READER, 
        dense=True, 
        autofocus=True, # <-- AGREGA ESTA LÍNEA AQUÍ
        on_change=lambda e: filtrar_y_renderizar_catalogo(), 
        on_submit=al_buscar_o_escanear_producto
    )

    # --- BLOQUE 5: COBRO DE VENTA, CARGA INICIAL Y RETORNO DE LA VISTA ---

    def cargar_productos_fijos():
        global productos_en_memoria
        try: 
            productos_en_memoria = db_obtener_productos()
        except Exception: 
            productos_en_memoria = []
        filtrar_y_renderizar_catalogo()

    def ejecutar_registro(referencia):
        if not carrito_chicha: return
        total_venta = sum(art.precio * art.cantidad for art in carrito_chicha)
        carrito_lista_compatible = []
        for art in carrito_chicha:
            unidades_descontar = float(art.cantidad * art.contenido_caja) if art.es_caja == 1 else float(art.cantidad)
            carrito_lista_compatible.append([art.id, art.nombre, art.precio, art.cantidad, art.es_caja, unidades_descontar])

        try:
            if db_registrar_venta_completa(carrito_lista_compatible, total_venta, dd_metodo.value, "ADMIN", referencia):
                carrito_chicha.clear()
                render_carrito()
                lbl_total.value = "Total: $0.00 (0.00 Bs.)"
                txt_buscar.value = ""
                cargar_productos_fijos()
                page.update()
        except NameError:
            page.snack_bar = ft.SnackBar(ft.Text("Error: db_registrar_venta_completa no está definida."), bgcolor="red")
            page.snack_bar.open = True
            page.update()

    def procesar_venta(e):
        if not carrito_chicha: return
        if dd_metodo.value == "PAGO MOVIL":
            txt_referencia.value = ""
            def guardar_con_referencia(ed):
                ref = txt_referencia.value.strip()
                dialogo_referencia.open = False
                page.update()
                ejecutar_registro(ref)
            dialogo_referencia.actions = [
                ft.TextButton("Cancelar", on_click=cerrar_dialogo_referencia), 
                ft.ElevatedButton("Confirmar", on_click=guardar_con_referencia)
            ]
            dialogo_referencia.open = True
            page.update()
        else: 
            ejecutar_registro("")

    btn_cobrar = ft.ElevatedButton(
        "✓ Cobrar Venta", 
        icon=ft.Icons.CHECK, 
        bgcolor=ft.Colors.GREEN_700, 
        color=ft.Colors.WHITE, 
        height=42, 
        expand=True, 
        on_click=procesar_venta
    )

    try:
        cargar_productos_fijos()
        
         
        
        return ft.Column([
            ft.Row([ft.Icon(ft.Icons.LOCAL_DRINK, color=ft.Colors.RED_ACCENT_700, size=24), ft.Text("CELER", size=16, weight=ft.FontWeight.BOLD)]),
            txt_buscar, 
            cuadricula_productos,  
            lista_productos_ui,    
            lista_carrito,
            ft.Column([ft.Row([dd_metodo, lbl_total], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), ft.Row([btn_cobrar])])
        ], expand=True, spacing=10)
    except Exception as err:
        return ft.Column([ft.Text(f"Error: {err}", color="red")])



# --- PANTALLA 4: FLUJO DE CAJA (TABLA TOTALMENTE RESPONSIVE) ---
def vista_caja(page: ft.Page):
    dd_tipo = ft.Dropdown(
        label="Tipo", 
        options=[ft.dropdown.Option("INGRESO"), ft.dropdown.Option("EGRESO")], 
        value="EGRESO"
    )
    txt_cat = ft.TextField(label="Categoría (Ej: Proveedores, Luz)")
    txt_desc = ft.TextField(label="Descripción")
    txt_monto = ft.TextField(label="Monto ($)", hint_text="Ej: 25.50") # Sin restricción estricta de teclado
    
    contenedor_datos = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO)

    def cargar_movimientos():
        movimientos = db_obtener_movimientos_caja()
        contenedor_datos.controls.clear()

        if page.width < 600:
            for m in movimientos:
                es_ingreso = m[1] == "INGRESO"
                color_tipo = ft.Colors.GREEN if es_ingreso else ft.Colors.RED
                icono_tipo = ft.Icons.ARROW_UPWARD if es_ingreso else ft.Icons.ARROW_DOWNWARD

                contenedor_datos.controls.append(
                    ft.Card(
                        content=ft.Container(
                            content=ft.Column([
                                ft.Row([
                                    ft.Row([
                                        ft.Icon(icono_tipo, color=color_tipo, size=16),
                                        ft.Text(str(m[1]), color=color_tipo, weight=ft.FontWeight.BOLD),
                                    ], spacing=5),
                                    ft.Text(f"${m[4]:.2f}", size=16, weight=ft.FontWeight.BOLD, color=color_tipo)
                                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                                ft.Divider(height=5, thickness=0.5),
                                ft.Text(f"Categoría: {m[2]}", size=14, weight=ft.FontWeight.W_500),
                                ft.Text(f"Nota: {m[3] if m[3] else '-'}", size=13, color=ft.Colors.GREY_600),
                                ft.Text(str(m[0]), size=11, color=ft.Colors.GREY_400, text_align=ft.TextAlign.RIGHT),
                            ], spacing=4),
                            padding=12
                        )
                    )
                )
        else:
            tabla_movs = ft.DataTable(
                expand=True,
                columns=[
                    ft.DataColumn(ft.Text("Fecha")), 
                    ft.DataColumn(ft.Text("Tipo")), 
                    ft.DataColumn(ft.Text("Categoría")), 
                    ft.DataColumn(ft.Text("Descripción")), 
                    ft.DataColumn(ft.Text("Monto"))
                ]
            )
            for m in movimientos:
                col_t = ft.Colors.GREEN if m[1] == "INGRESO" else ft.Colors.RED
                tabla_movs.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(str(m[0]))), 
                    ft.DataCell(ft.Text(str(m[1]), color=col_t, weight=ft.FontWeight.BOLD)), 
                    ft.DataCell(ft.Text(str(m[2]))), 
                    ft.DataCell(ft.Text(str(m[3]) if m[3] else "-")), 
                    ft.DataCell(ft.Text(f"${m[4]:.2f}"))
                ]))
            contenedor_datos.controls.append(tabla_movs)
        page.update()

    # 1º DECLARAMOS LA FUNCIÓN (Para que Python sepa que existe)
    def registrar_movimiento(e):
        if not all([txt_cat.value, txt_monto.value]): return
        try:
            monto_texto = txt_monto.value.strip().replace(",", ".")
            monto_val = float(monto_texto)
            
            if monto_val <= 0:
                page.open(ft.SnackBar(ft.Text("El monto debe ser mayor a cero."), bgcolor="red"))
                return

            db_insertar_movimiento_caja(dd_tipo.value, txt_cat.value.strip(), txt_desc.value.strip(), monto_val)
            txt_cat.value = ""
            txt_desc.value = ""
            txt_monto.value = ""
            cargar_movimientos()
        except ValueError:
            page.open(ft.SnackBar(
                ft.Text("Error: Ingresa un monto numérico válido (puedes usar punto o coma)."),
                bgcolor="red"
            ))

    # 2º CREAMOS EL BOTÓN (Ahora sí encuentra 'registrar_movimiento' sin problemas)
    btn_registrar = ft.ElevatedButton(
        "Registrar Movimiento", 
        on_click=registrar_movimiento, 
        bgcolor=ft.Colors.BLUE, 
        color=ft.Colors.WHITE
    )

    page.on_resize = lambda e: cargar_movimientos()

    formulario_responsive = ft.ResponsiveRow([
        ft.Container(dd_tipo, col={"xs": 12, "md": 2}),
        ft.Container(txt_cat, col={"xs": 12, "md": 4}),
        ft.Container(txt_desc, col={"xs": 12, "md": 4}),
        ft.Container(txt_monto, col={"xs": 12, "md": 2}),
    ], spacing=10)

    cargar_movimientos()

    return ft.Column([
        ft.Text("💰 Flujo de Caja Manual", size=26, weight=ft.FontWeight.BOLD),
        ft.Divider(),
        formulario_responsive, 
        ft.ResponsiveRow([
            ft.Container(btn_registrar, col={"xs": 12, "md": 3}, alignment=ft.alignment.Alignment(1, 0))
        ]),
        ft.Divider(),
        contenedor_datos 
    ], expand=True, scroll=ft.ScrollMode.AUTO)


# --- PANTALLA 5: HISTORIAL DE VENTAS COMPACTO CON CHECKBOX UNIVERSAL (PARTE 1 REPARADA) ---
import datetime
import flet as ft

def vista_historial_ventas(page: ft.Page):
    lista_historial = ft.ListView(spacing=5, height=500, scroll=ft.ScrollMode.ALWAYS, auto_scroll=False)

    # SOLUCIÓN AL BANNER ROJO: Inicializamos el diálogo con datos base para que Flet lo acepte al cargar
    dialogo_comun = ft.AlertDialog(
        modal=True,
        title=ft.Text("Cargando..."),
        content=ft.Text("Espere un momento..."),
        actions=[ft.TextButton("Ok")]
    )
    page.overlay.append(dialogo_comun)

    def cerrar_cualquier_dialogo(e=None):
        dialogo_comun.open = False
        page.update()

    def generar_tarjeta_venta(id_venta, fecha_venta, total_venta, metodo_pago, tasa_dolar, vendedor_ticket, referencia_pm, conciliado_inicial):
        try:
            total_bs = float(total_venta) * float(tasa_dolar)
        except Exception:
            total_bs = 0.0

        folio_str = f"# {id_venta}"
        vendedor_ticket = vendedor_ticket or "ADMIN"
        
        texto_metodo = str(metodo_pago)
        if metodo_pago == "PAGO MOVIL" and referencia_pm:
            texto_metodo += f" (Ref: {referencia_pm})"

        columna_productos = ft.Column(spacing=3, tight=True)
        
        try:
            articulos = db_obtener_detalles_productos_venta(id_venta)
        except Exception:
            articulos = []
            
        if not articulos:
            columna_productos.controls.append(
                ft.Text("⚠ No se encontraron artículos.", size=12, color=ft.Colors.RED_700, italic=True)
            )
        else:
            for art in articulos:
                try:
                    nombre_p, cantidad, precio_u = art
                    sub = cantidad * precio_u
                    columna_productos.controls.append(
                        ft.Row([
                            ft.Text(f"• {cantidad}x {nombre_p}", size=16, color=ft.Colors.BLUE_GREY_700, expand=True),
                            ft.Text(f"${sub:.2f}", size=16, weight=ft.FontWeight.W_500, color=ft.Colors.BLUE_GREY_900)
                        ])
                    )
                except Exception:
                    continue
        
        contenedor_detalle = ft.Container(
            content=ft.Column([
                ft.Divider(height=10, thickness=1, color=ft.Colors.GREY_300),
                ft.Text("Artículos Facturados:", size=11, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_ACCENT_700),
                columna_productos
            ]),
            visible=False,
            padding=ft.Padding(10, 0, 10, 5)
        )
        
        def conmutar_detalle(e):
            contenedor_detalle.visible = not contenedor_detalle.visible
            try:
                e.control.icon = ft.Icons.KEYBOARD_ARROW_UP if contenedor_detalle.visible else ft.Icons.INFO
            except Exception:
                pass
            page.update()

        # --- BOTÓN BORRAR VENTA CORREGIDO ---
        def presionar_eliminar_venta(e):
            def confirmar_eliminacion(ev):
                exito = db_eliminar_venta_y_reponer_inventario(id_venta)
                cerrar_cualquier_dialogo()
                if exito:
                    page.snack_bar = ft.SnackBar(ft.Text(f"Venta {folio_str} eliminada. Inventario devuelto."))
                    page.snack_bar.open = True
                    cargar_historial()
                else:
                    page.snack_bar = ft.SnackBar(ft.Text("Error al eliminar la venta de la base de datos."), bgcolor=ft.Colors.RED_700)
                    page.snack_bar.open = True
                    page.update()

            dialogo_comun.title = ft.Text("🚨 ¿Eliminar esta venta?")
            dialogo_comun.content = ft.Text(
                f"Esta acción borrará permanentemente el Folio {folio_str}.\n"
                "Los productos volverán al stock automáticamente."
            )
            dialogo_comun.actions = [
                ft.TextButton("Cancelar", on_click=cerrar_cualquier_dialogo),
                ft.ElevatedButton("Sí, eliminar", bgcolor=ft.Colors.RED_600, color=ft.Colors.WHITE, on_click=confirmar_eliminacion)
            ]
            dialogo_comun.actions_alignment = ft.MainAxisAlignment.END
            dialogo_comun.open = True
            page.update()

        chk_banco = ft.Checkbox(
            value=True if conciliado_inicial == 1 else False,
            fill_color=ft.Colors.GREEN_600,
        )

        # --- CHECKBOX CORREGIDO ---
        def cambiar_estado_checkbox(e):
            if chk_banco.value == False:
                def confirmar_destildar(ev):
                    db_actualizar_conciliacion(id_venta, 0)
                    chk_banco.value = False
                    cerrar_cualquier_dialogo()

                def cancelar_destildar(ev):
                    chk_banco.value = True
                    cerrar_cualquier_dialogo()

                dialogo_comun.title = ft.Text("⚠️ Advertencia de Control")
                dialogo_comun.content = ft.Text(f"¿Deseas quitar la verificación del Folio {folio_str}? Volverá a marcarse como pendiente.")
                dialogo_comun.actions = [
                    ft.TextButton("Cancelar", on_click=cancelar_destildar),
                    ft.ElevatedButton("Sí, quitar", bgcolor=ft.Colors.RED_600, color=ft.Colors.WHITE, on_click=confirmar_destildar)
                ]
                dialogo_comun.actions_alignment = ft.MainAxisAlignment.END
                dialogo_comun.open = True
                page.update()
            else:
                db_actualizar_conciliacion(id_venta, 1)
                chk_banco.value = True
                page.update()

        chk_banco.on_change = cambiar_estado_checkbox

        try:
            total_float = float(total_venta)
        except Exception:
            total_float = 0.0

        return ft.Card(
            content=ft.Container(
                content=ft.Column([
                    ft.Row([
                        chk_banco,
                        ft.Icon(ft.Icons.RECEIPT, color=ft.Colors.BLUE_GREY, size=22),
                        ft.Column([
                            ft.Text(f"Folio {folio_str} - {texto_metodo}", weight=ft.FontWeight.BOLD, size=16),
                            #ft.Text(f"Atendido por: {vendedor_ticket}", size=11, color=ft.Colors.BLUE_GREY_500, weight=ft.FontWeight.W_500),
                            ft.Text(str(fecha_venta), size=16, color=ft.Colors.GREY_600)
                        ], spacing=1, expand=True),
                        ft.Column([
                            ft.Text(f"${total_float:.2f}", weight=ft.FontWeight.BOLD, size=16, color=ft.Colors.GREEN_700),
                            ft.Text(f"{total_bs:.1f} Bs.", size=15, color=ft.Colors.BLUE_900)
                        ], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=1),
                        
                        ft.Row([
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE, 
                                icon_size=20, 
                                icon_color=ft.Colors.RED_400,
                                tooltip="Eliminar venta erronea",
                                on_click=presionar_eliminar_venta
                            ),
                            ft.IconButton(
                                icon=ft.Icons.INFO, 
                                icon_size=20, 
                                icon_color=ft.Colors.BLUE,
                                on_click=conmutar_detalle
                            )
                        ], spacing=0)
                    ]),
                    contenedor_detalle
                ]),
                padding=10
            )
        ) 

#  HISTORIAL DE VENTAS COMPACTO CON CHECKBOX UNIVERSAL (PARTE 2 REPARADA) ---
# Pega esto exactamente debajo del Bloque 1, alineado al mismo nivel de la función vista_historial_ventas

    def cargar_historial():
        lista_historial.controls.clear()
        try:
            tasa = float(db_obtener_tasa_dolar())
        except Exception:
            tasa = 1.0
        
        try:
            ventas = db_obtener_historial_ventas()
        except Exception:
            ventas = []
        
        if not ventas:
            lista_historial.controls.append(ft.Text("No hay ventas registradas.", size=14, color="grey"))
            page.update()
            return

        for v in ventas:
            try:
                # COMPROBADO Y REPARADO: Indexamos de forma correcta la tupla devuelta por SQLite
                id_v = v[0]
                fecha = v[1]
                
                try:
                    total = float(v[2])
                except (IndexError, ValueError, TypeError):
                    total = 0.0

                metodo = v[3] if len(v) > 3 else "PUNTO"
                vendedor = v[4] if len(v) > 4 and v[4] else "ADMIN"
                referencia = v[5] if len(v) > 5 and v[5] else ""
                
                try:
                    conciliado = int(v[6]) if len(v) > 6 and v[6] is not None else 0
                except Exception:
                    conciliado = 0
                
                tarjeta = generar_tarjeta_venta(id_v, fecha, total, metodo, tasa, vendedor, referencia, conciliado)
                
                # 📍 ¡ESTA LÍNEA ES LA MAGIA! Guardamos la fecha pura en la raíz de la tarjeta

                tarjeta.data = str(fecha)

                lista_historial.controls.append(tarjeta)
            except Exception as e_fila:
                print(f"Fila ignorada por error menor: {e_fila}")
                continue
        page.update()

    texto_boton_fecha = ft.Text("Seleccionar Día", size=13, weight=ft.FontWeight.W_500)

    btn_fecha_texto = ft.ElevatedButton(
        content=texto_boton_fecha,
        icon="calendar_month",
        on_click=lambda _: page.show_dialog(picker_historial) if hasattr(page, "show_dialog") else page.open(picker_historial)
    )

    def al_cambiar_fecha(e):
        if picker_historial.value:
            # Convierte la fecha seleccionada en el calendario a formato AAAA-MM-DD
            fecha_elegida = picker_historial.value.date().strftime("%Y-%m-%d")
            texto_boton_fecha.value = f"Día: {picker_historial.value.date().strftime('%d/%m/%Y')}"
            texto_boton_fecha.update()
            btn_reset.visible = True
            btn_reset.update()
            
            for tarjeta in lista_historial.controls:
                # Leemos el dato puro de la fecha que inyectamos en el Paso 1
                fecha_tarjeta = tarjeta.data
                
                if fecha_tarjeta:
                    # Compara si el día elegido está contenido en la fecha/hora de la tarjeta
                    tarjeta.visible = (fecha_elegida in str(fecha_tarjeta))
                else:
                    tarjeta.visible = False
                    
            lista_historial.update()

    def restablecer_todo(e):
        texto_boton_fecha.value = "Seleccionar Día"
        texto_boton_fecha.update()
        btn_reset.visible = False
        btn_reset.update()
        for tarjeta in lista_historial.controls:
            tarjeta.visible = True
        lista_historial.update()

    btn_reset = ft.IconButton(
        icon="close",
        icon_color="red",
        tooltip="Quitar Filtro",
        visible=False,
        on_click=restablecer_todo
    )

    def mostrar_reporte_vendedores_click(e):
        reporte_sumas = {}
        for tarjeta in lista_historial.controls:
            try:
                if not tarjeta.visible:
                    continue
                
                fila_principal = tarjeta.content.content.controls[0]
                
                # Extrae los nombres de vendedores de manera limpia y segura
                texto_vendedor_crudo = fila_principal.controls[2].controls[1].value
                vendedor = str(texto_vendedor_crudo).replace("Atendido por:", "").strip().upper()
                if not vendedor:
                    vendedor = "ADMIN"
                
                # Extrae los precios respetando los nuevos layouts
                texto_precio_crudo = fila_principal.controls[3].controls[0].value
                monto_usd = float(str(texto_precio_crudo).replace("$", "").strip())
                
                if vendedor in reporte_sumas:
                    reporte_sumas[vendedor] += monto_usd
                else:
                    reporte_sumas[vendedor] = monto_usd
            except Exception:
                continue

        filas_vendedores_ui = []
        if not reporte_sumas:
            filas_vendedores_ui.append(
                ft.Text("No hay registros visibles para sumar.", color=ft.Colors.GREY_600, italic=True)
            )
        else:
            for vend, total_usd in reporte_sumas.items():
                filas_vendedores_ui.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Row([
                                ft.Icon(ft.Icons.ACCOUNT_CIRCLE, color=ft.Colors.BLUE_400),
                                ft.Text(f"{vend}", weight=ft.FontWeight.BOLD, size=13)
                            ], spacing=8),
                            ft.Text(f"${total_usd:.2f}", color=ft.Colors.GREEN_700, weight=ft.FontWeight.BOLD, size=14)
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        padding=ft.Padding(10, 6, 10, 6),
                        border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.GREY_200))
                    )
                )

        # REUTILIZACIÓN DEL DIÁLOGO COMÚN PARA EL REPORTE
        dialogo_comun.title = ft.Row([
            ft.Icon(ft.Icons.ASSESSMENT_ROUNDED, color=ft.Colors.GREEN_700),
            ft.Text("Sumatoria de Ventas ($)", size=16, weight=ft.FontWeight.BOLD)
        ], spacing=10)
        dialogo_comun.content = ft.Column(
            controls=filas_vendedores_ui,
            tight=True,
            width=320,
            scroll=ft.ScrollMode.AUTO
        )
        dialogo_comun.actions = [
            #ft.TextButton("Cerrar", on_click=cerrar_cualquier_dialogo)
        ]
        dialogo_comun.actions_alignment = ft.MainAxisAlignment.END
        dialogo_comun.open = True
        page.update()

    btn_reporte = ft.IconButton(
        icon=ft.Icons.BAR_CHART_ROUNDED,
        icon_color=ft.Colors.GREEN_700,
        tooltip="Ver totales por vendedor",
        #on_click=mostrar_reporte_vendedores_click
    )

    picker_historial = ft.DatePicker(
        on_change=al_cambiar_fecha,
        first_date=datetime.datetime(2024, 1, 1),
        last_date=datetime.datetime(2030, 12, 31)
    )
    page.overlay.append(picker_historial)

    try:
        cargar_historial()
        return ft.Column([
            # Usamos una fila principal con envoltorio fluido (wrap=True) para móviles
            ft.Row([
                # 🔄 CORRECCIÓN CRÍTICA: Eliminamos expand=True para evitar que el texto
                # colapse letra por letra hacia abajo en pantallas angostas.
                ft.Text(
                    "📜 Historial de Ventas Y Conciliación Bancaria", 
                    size=20, 
                    weight=ft.FontWeight.BOLD
                ),
                
                # Bloque contenedor de tus botones de acción de filtrado
                ft.Row([
                    #btn_reporte,
                    btn_fecha_texto, 
                    btn_reset
                ], 
                alignment=ft.MainAxisAlignment.START, # Alineación fluida para el salto de línea
                spacing=5
                )
            ], 
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,         # Permite que los botones bajen si no hay espacio
            run_spacing=10     # Separa el título de los botones cuando bajen en el móvil
            ),
            
            ft.Divider(height=10, color=ft.Colors.TRANSPARENT), # Separador sutil de seguridad
            lista_historial
        ], expand=False)
    except Exception as error_general:
        return ft.Column([
            ft.Text("⚠ Error crítico al cargar la interfaz", size=16, color="red", weight="bold"),
            ft.Text(str(error_general), color="red")
        ])


# --- PANTALLA 6: CONFIGURACIÓN GENERAL CON ACCESO A PERSONAL ---
# --- SUB-INTERFAZ MODAL: GESTIÓN DE PERSONAL (DESLIZANTE PARA MÓVIL) ---
def abrir_modal_usuarios(page: ft.Page):
    txt_new_user = ft.TextField(label="Nombre de Usuario (Login)", dense=True)
    txt_new_pass = ft.TextField(label="Contraseña", password=True, can_reveal_password=True, dense=True)
    dd_rol = ft.Dropdown(
        label="Rol del Usuario",
        options=[ft.dropdown.Option("vendedor"), ft.dropdown.Option("master")],
        value="vendedor",
        dense=True
    )
    
    lista_personal = ft.ListView(expand=True, spacing=5, height=220)

    def refrescar_lista_usuarios():
        lista_personal.controls.clear()
        usuarios = db_obtener_todos_usuarios()
        
        for u in usuarios:
            id_u, username, rol = u
            puede_borrar = username.lower() != "admin"
            id_actual = id_u
            name_actual = username

            lista_personal.controls.append(
                ft.Card(
                    content=ft.Container(
                        content=ft.Row([
                            ft.Icon(
                                ft.Icons.ACCOUNT_CIRCLE if rol == "master" else ft.Icons.SUPPORT_AGENT, 
                                color=ft.Colors.BLUE_800 if rol == "master" else ft.Colors.BLUE_GREY_600
                            ),
                            ft.Column([
                                ft.Text(username.upper(), weight=ft.FontWeight.BOLD, size=13),
                                ft.Text(f"Rol: {rol.upper()}", size=10, color=ft.Colors.GREY_600)
                            ], spacing=1, expand=True),
                            
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=ft.Colors.RED_400,
                                icon_size=18,
                                visible=puede_borrar,
                                on_click=lambda e, idx=id_actual, nom=name_actual: ejecutar_baja(idx, nom)
                            )
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        padding=8
                    ),
                    elevation=1
                )
            )
        page.update()

    def ejecutar_baja(id_u, nombre_u):
        db_eliminar_usuario(id_u)
        refrescar_lista_usuarios()

    def registrar_usuario_click(e):
        u = txt_new_user.value.strip().lower()
        p = txt_new_pass.value.strip()
        
        if not u or not p: return
            
        exito = db_insertar_usuario(u, p, dd_rol.value)
        if exito:
            txt_new_user.value = ""
            txt_new_pass.value = ""
            refrescar_lista_usuarios()

    btn_crear = ft.ElevatedButton(
        "Registrar", 
        icon=ft.Icons.PERSON_ADD, 
        on_click=registrar_usuario_click,
        bgcolor=ft.Colors.GREEN_700,
        color=ft.Colors.WHITE
    )

    refrescar_lista_usuarios()

    # Construimos el BottomSheet deslizante
    modal = ft.BottomSheet(
        content=ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text("👥 Control de Personal y Accesos", size=16, weight=ft.FontWeight.BOLD),
                    ft.IconButton(ft.Icons.CLOSE, on_click=lambda e: cerrar_modal())
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(height=5),
                txt_new_user,
                txt_new_pass,
                dd_rol,
                ft.Row([btn_crear], alignment=ft.MainAxisAlignment.END),
                ft.Divider(height=5),
                ft.Text("Plantilla Activa:", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_700),
                lista_personal
            ], spacing=10, scroll=ft.ScrollMode.AUTO),
            padding=20,
        ),
        dismissible=True
    )

    def cerrar_modal():
        modal.open = False
        page.update()

    page.overlay.append(modal)
    modal.open = True
    page.update()




def vista_configuracion(page: ft.Page):
    tasa_actual = db_obtener_tasa_dolar()
    
    txt_tasa = ft.TextField(
        label="Tasa del Dólar (Bs. / USD)", 
        value=f"{tasa_actual:.2f}", 
        width=220,
        text_align=ft.TextAlign.CENTER,
        hint_text="Ej: 36.50"
    )
    
    lbl_status = ft.Text("", size=14)

    # --- LOGICA DEL SELECTOR DE EXCEL CORREGIDA ---
    def al_seleccionar_archivo_excel(e):
        if not e.files:
            return
        
        ruta_excel = e.files[0].path
        # Llamamos a tu función de base de datos
        exito = db_importar_inventario_excel(ruta_excel)
        
        # --- CORRECCIÓN PARA FLET 0.86.5 ---
        if exito:
            page.show_dialog(
                ft.SnackBar(
                    content=ft.Text("¡Inventario importado y actualizado con éxito desde Excel!"), 
                    bgcolor="green"
                )
            )
        else:
            page.show_dialog(
                ft.SnackBar(
                    content=ft.Text("Error al procesar el archivo Excel. Revisa el formato."), 
                    bgcolor="red"
                )
            )
        
        page.update()

    def ejecutar_carga_masiva_fija(e):
        import os
        
        # 1. Definición estricta de la ruta absoluta
        try:
            ruta_actual = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            ruta_actual = os.getcwd()
            
        archivo_fijo = os.path.join(ruta_actual, "inventario.xlsx")
        
        # 2. Comprobación física del archivo
        if not os.path.exists(archivo_fijo):
            # CORRECCIÓN PARA FLET 0.86+: Se despliega usando show_dialog()
            page.show_dialog(
                ft.SnackBar(
                    content=ft.Text(f"Falta el archivo en: {archivo_fijo}"), 
                    bgcolor="orange"
                )
            )
            page.update()
            return
            
        # 3. Procesamiento seguro de datos
        try:
            exito = db_importar_inventario_excel(archivo_fijo)
            if exito:
                page.show_dialog(
                    ft.SnackBar(
                        content=ft.Text("¡Inventario cargado y sumado con éxito desde Excel!"), 
                        bgcolor="green"
                    )
                )
            else:
                page.show_dialog(
                    ft.SnackBar(
                        content=ft.Text("Error: El archivo no tiene el formato o columnas correctas."), 
                        bgcolor="red"
                    )
                )
        except Exception as error_lectura:
            page.show_dialog(
                ft.SnackBar(
                    content=ft.Text(f"Error crítico en lectura: {error_lectura}"), 
                    bgcolor="red"
                )
            )
        
        page.update()

    btn_cargar_excel = ft.ElevatedButton(
        "Procesar archivo 'inventario.xlsx'",
        icon=ft.Icons.UPLOAD_FILE_ROUNDED,
        bgcolor=ft.Colors.GREEN_800,
        color=ft.Colors.WHITE,
        on_click=ejecutar_carga_masiva_fija
    )

  
    def guardar_tasa_click(e):
        try:
            tasa_texto = txt_tasa.value.strip().replace(",", ".")
            valor = float(tasa_texto)
            if valor <= 0:
                lbl_status.value = "Error: La tasa debe ser mayor a 0."
                lbl_status.color = ft.Colors.RED
            else:
                db_actualizar_tasa_dolar(valor)
                lbl_status.value = f"¡Tasa actualizada con éxito a {valor:.2f} Bs.!"
                lbl_status.color = ft.Colors.GREEN_700
                txt_tasa.value = f"{valor:.2f}"
        except ValueError:
            lbl_status.value = "Error: Ingresa un número decimal válido."
            lbl_status.color = ft.Colors.RED
        page.update()

    btn_guardar_tasa = ft.ElevatedButton(
        "Actualizar Tasa", 
        icon=ft.Icons.SAVE, 
        on_click=guardar_tasa_click,
        bgcolor=ft.Colors.BLUE_800,
        color=ft.Colors.WHITE
    )

    # --- NUEVA LÓGICA DE ARCHIVO PARA EL DISEÑO HÍBRIDO (FLET 0.86.5) ---
    import os
    ruta_txt_config = "config_vista.txt"
    if not os.path.exists(ruta_txt_config):
        with open(ruta_txt_config, "w") as f:
            f.write("cuadricula")

    texto_boton_vista = ft.Text("", color=ft.Colors.WHITE, weight=ft.FontWeight.W_500)

    def cambiar_diseño_vista(e):
        with open(ruta_txt_config, "r") as f:
            actual = f.read().strip()
        nuevo_modo = "lista" if actual == "cuadricula" else "cuadricula"
        with open(ruta_txt_config, "w") as f:
            f.write(nuevo_modo)
            
        texto_boton_vista.value = f"Modo Actual: {nuevo_modo.upper()}"
        btn_cambiar.icon = ft.Icons.LIST_ALT if nuevo_modo == "lista" else ft.Icons.GRID_VIEW
        
        # CORREGIDO PARA FLET 0.86.5: Se lanza el SnackBar a la antigua
        page.snack_bar = ft.SnackBar(ft.Text(f"Diseño cambiado a modo {nuevo_modo}"), bgcolor="green")
        page.snack_bar.open = True
        page.update()

    with open(ruta_txt_config, "r") as f:
        modo_actual = f.read().strip()

    texto_boton_vista.value = f"Modo Actual: {modo_actual.upper()}"

    btn_cambiar = ft.ElevatedButton(
        content=texto_boton_vista,
        icon=ft.Icons.GRID_VIEW if modo_actual == "cuadricula" else ft.Icons.LIST_ALT,
        bgcolor=ft.Colors.BLUE_GREY_800,
        on_click=cambiar_diseño_vista
    )

    return ft.Column([
        ft.Text("⚙ Panel de Configuración", size=26, weight=ft.FontWeight.BOLD),
        ft.Text("Define los parámetros del sistema y conversión monetaria.", size=14, color=ft.Colors.GREY_700),
        ft.Divider(),
        ft.Container(height=5),
        
        # TARJETA 1: TASA DEL DÓLAR
        ft.Card(
            ft.Container(
                content=ft.Column([
                    ft.Text("Ajuste de Moneda (Venezuela)", weight=ft.FontWeight.BOLD, size=16),
                    ft.Text("Los precios de ventas e inventarios se calcularán usando este valor.", size=12, color=ft.Colors.GREY_600),
                    ft.Container(height=5),
                    ft.Row([txt_tasa, btn_guardar_tasa], alignment=ft.MainAxisAlignment.START, spacing=15, wrap=True),
                    lbl_status
                ]),
                padding=15
            ),
            elevation=2
        ),
        
        ft.Container(height=10),
        
        # TARJETA 2: ACCESO GRÁFICO AL PERSONAL
        ft.Card(
            ft.Container(
                content=ft.Column([
                    ft.Text("Seguridad y Empleados", weight=ft.FontWeight.BOLD, size=16),
                    ft.Text("Crea nuevos perfiles para tus vendedores o da de baja cuentas existentes.", size=12, color=ft.Colors.GREY_600),
                    ft.Container(height=5),
                    ft.ElevatedButton(
                        "Administrar Personal",
                        icon=ft.Icons.MANAGE_ACCOUNTS,
                        bgcolor=ft.Colors.BLUE_GREY_800,
                        color=ft.Colors.WHITE,
                        on_click=lambda e: abrir_modal_usuarios(page)
                    )
                ]),
                padding=15
            ),
            elevation=2
        ),

        ft.Container(height=10),

        # NUEVA TARJETA 3: DISEÑO DEL PUNTO DE VENTA
        ft.Card(
            ft.Container(
                content=ft.Column([
                    ft.Text("Diseño del Catálogo de Ventas", weight=ft.FontWeight.BOLD, size=16),
                    ft.Text("Cambia la forma en que tus vendedores visualizan los productos disponibles.", size=12, color=ft.Colors.GREY_600),
                    ft.Container(height=5),
                    btn_cambiar
                ]),
                padding=15
            ),
            elevation=2
        ),


        #ft.Container(height=10),

        # NUEVA TARJETA 4: IMPORTACIÓN MASIVA DESDE EXCEL
        #ft.Card(
            #ft.Container(
                #content=ft.Column([
                    #ft.Text("Carga Masiva de Inventario", weight=ft.FontWeight.BOLD, size=16),
                    #ft.Text("Sube un archivo .xlsx para registrar nuevos productos o sumar stock a los existentes de forma masiva.", size=12, #color=ft.Colors.GREY_600),
                    #ft.Container(height=5),
                    #btn_cargar_excel
                #]),
                #padding=15
            #),
            #elevation=2
        #)
    ], expand=True, scroll=ft.ScrollMode.ALWAYS)




# =====================================================================
# =====================================================================
# 3. PARTE: ENRUTAMIENTO GENERAL Y MENÚ (MAIN OPTIMIZADO PARA MÓVIL)
# =====================================================================

def main(page: ft.Page):
    page.title = "CELER"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.scroll = None
    page.padding = 0  

    global RUTA_DB
    if hasattr(page, "data_dir") and page.data_dir:
        RUTA_DB = os.path.join(page.data_dir, "sistema_negocio.db")
    else:
        RUTA_DB = "sistema_negocio.db"

    inicializar_base_de_datos()

    usuario_estado = {"rol": None, "username": None}

    contenedor_principal = ft.Container(expand=True, padding=2)
    divisor_vertical = ft.VerticalDivider(width=1, visible=False)

    # --- MENÚS DE NAVEGACIÓN RESTAURADOS A 6 PESTAÑAS ---
    menu_lateral = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=85,
        min_extended_width=200,
        leading=ft.Icon(ft.Icons.HOME, size=35, color=ft.Colors.BLUE_800),
        visible=False,
        destinations=[
            ft.NavigationRailDestination(icon=ft.Icons.POINT_OF_SALE_OUTLINED, selected_icon=ft.Icons.POINT_OF_SALE, label="Ventas"),
            ft.NavigationRailDestination(icon=ft.Icons.DASHBOARD_OUTLINED, selected_icon=ft.Icons.DASHBOARD, label="Finanzas"),
            ft.NavigationRailDestination(icon=ft.Icons.INVENTORY_2_OUTLINED, selected_icon=ft.Icons.INVENTORY_2, label="Inventario"),
            ft.NavigationRailDestination(icon=ft.Icons.ATTACH_MONEY_OUTLINED, selected_icon=ft.Icons.ATTACH_MONEY, label="Caja"),
            ft.NavigationRailDestination(icon=ft.Icons.HISTORY_OUTLINED, selected_icon=ft.Icons.HISTORY, label="Historial"),
            ft.NavigationRailDestination(icon=ft.Icons.SETTINGS_OUTLINED, selected_icon=ft.Icons.SETTINGS, label="Config."),
        ],
    )

    menu_inferior = ft.NavigationBar(
        selected_index=0,
        visible=False,
        height=65,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.POINT_OF_SALE_OUTLINED, selected_icon=ft.Icons.POINT_OF_SALE, label="Ventas"),
            ft.NavigationBarDestination(icon=ft.Icons.DASHBOARD_OUTLINED, selected_icon=ft.Icons.DASHBOARD, label="Finanzas"),
            ft.NavigationBarDestination(icon=ft.Icons.INVENTORY_2_OUTLINED, selected_icon=ft.Icons.INVENTORY_2, label="Inventario"),
            ft.NavigationBarDestination(icon=ft.Icons.ATTACH_MONEY_OUTLINED, selected_icon=ft.Icons.ATTACH_MONEY, label="Caja"),
            ft.NavigationBarDestination(icon=ft.Icons.HISTORY_OUTLINED, selected_icon=ft.Icons.HISTORY, label="Historial"),
            ft.NavigationBarDestination(icon=ft.Icons.SETTINGS_OUTLINED, selected_icon=ft.Icons.SETTINGS, label="Config."),
        ],
    )
    def cambiar_pantalla(e):
        opcion = int(e.control.selected_index)
        menu_lateral.selected_index = opcion
        menu_inferior.selected_index = opcion
        
        vistas = {
            0: lambda: vista_ventas(page),
            1: lambda: vista_dashboard(page),
            2: lambda: vista_inventario(page),
            3: lambda: vista_caja(page),
            4: lambda: vista_historial_ventas(page),
            5: lambda: vista_configuracion(page)
        }
        
        nueva_vista = vistas.get(opcion, lambda: vista_ventas(page))()
        contenedor_principal.content = ft.Column([nueva_vista], scroll=ft.ScrollMode.AUTO, expand=True)
        page.update()

    menu_lateral.on_change = cambiar_pantalla
    menu_inferior.on_change = cambiar_pantalla

    def adaptar_interfaz_por_ancho(e=None):
        rol = usuario_estado["rol"]
        if not rol or rol == "vendedor":
            return

        if page.width < 600:
            menu_lateral.visible = False
            divisor_vertical.visible = False
            page.navigation_bar = menu_inferior
            menu_inferior.visible = True
        else:
            menu_lateral.visible = True
            divisor_vertical.visible = True
            page.navigation_bar = None
            menu_inferior.visible = False
        page.update()

    page.on_resize = adaptar_interfaz_por_ancho

    def cerrar_sesion(e):
        usuario_estado["rol"] = None
        usuario_estado["username"] = None
        menu_lateral.visible = False
        divisor_vertical.visible = False
        menu_inferior.visible = False
        page.navigation_bar = None
        contenedor_principal.content = estructura_login
        page.update()

    btn_logout = ft.IconButton(icon=ft.Icons.LOGOUT, icon_color="red", on_click=cerrar_sesion, tooltip="Cerrar Sesión")

    def inicializar_interfaz_sistema(rol_usuario, username):
        usuario_estado["rol"] = rol_usuario
        usuario_estado["username"] = username

        if rol_usuario == "vendedor":
            menu_lateral.visible = False
            divisor_vertical.visible = False
            menu_inferior.visible = False
            page.navigation_bar = None
            
            header_vendedor = ft.Row([
                ft.Text(f"👤 Vendedor: {username.upper()}", weight=ft.FontWeight.BOLD, size=14),
                btn_logout
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            
            contenedor_principal.content = ft.Column([
                header_vendedor,
                vista_ventas(page)
            ], scroll=ft.ScrollMode.AUTO, expand=True)
        
        elif rol_usuario == "master":
            menu_lateral.selected_index = 0
            menu_inferior.selected_index = 0
            menu_lateral.trailing = btn_logout
            
            adaptar_interfaz_por_ancho()
            contenedor_principal.content = ft.Column([vista_ventas(page)], scroll=ft.ScrollMode.AUTO, expand=True)

        page.update()

    # --- PANTALLA GRÁFICA DE LOGIN ---
    txt_user = ft.TextField(label="Usuario", icon=ft.Icons.PERSON, width=280)
    txt_pass = ft.TextField(label="Contraseña", icon=ft.Icons.LOCK, password=True, can_reveal_password=True, width=280)
    lbl_error = ft.Text("", color="red", size=12)

    def intentar_login(e):
        u = txt_user.value.strip()
        p = txt_pass.value.strip()

        if not u or not p:
            lbl_error.value = "Por favor, llena todos los campos."
            page.update()
            return

        usuario_validado = db_verificar_credenciales(u, p)

        if usuario_validado:
            rol_db, name_db = usuario_validado
            inicializar_interfaz_sistema(rol_usuario=rol_db, username=name_db)
        else:
            lbl_error.value = "Usuario o contraseña incorrectos."
            page.update()

    btn_entrar = ft.ElevatedButton(
        "Ingresar al Sistema", 
        on_click=intentar_login, 
        bgcolor=ft.Colors.BLUE_800, 
        color=ft.Colors.WHITE, 
        width=280,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))
    )

    tarjeta_login = ft.Card(
        content=ft.Container(
            content=ft.Column([
                ft.Text("CELER APP", size=40, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_900),
                ft.Text("SOFTWARE LADERA", size=14, color=ft.Colors.BLACK, weight=ft.FontWeight.BOLD),
                ft.Container(height=10),
                txt_user,
                txt_pass,
                lbl_error,
                ft.Container(height=10),
                btn_entrar
            ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=25,
            width=320,
        ),
        elevation=5
    )

    estructura_login = ft.Column([
        ft.Row([
            tarjeta_login
        ], alignment=ft.MainAxisAlignment.CENTER)
    ], alignment=ft.MainAxisAlignment.CENTER, expand=True)

    contenedor_principal.content = estructura_login

    estructura_completa = ft.Row([
        menu_lateral,
        divisor_vertical,
        contenedor_principal,
    ], expand=True)

    page.add(estructura_completa)

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8080))
    # Esta es la sintaxis oficial y limpia para la versión actual de Flet en web
    ft.app(target=main, view=ft.AppView.WEB_BROWSER, host="0.0.0.0", port=port)
