import flet as ft
import os
from flet import Icons
import libsql
import shutil
from datetime import datetime



# =====================================================================
# 1. PARTE: CONFIGURACIÓN Y FUNCIONES DE LA BASE DE DATOS (SQLITE)
# =====================================================================

def inicializar_base_datos():
    """Crea la conexión remota a Turso y las tablas si no existen."""
    # 1. Intentamos leer desde Vercel
    db_url = os.getenv("TURSO_DATABASE_URL")
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip('"' "'")
    

        
    conn = libsql.connect(database=db_url, auth_token=auth_token)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    
    # Tabla: Usuarios (Roles: 'master' o 'vendedor')
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        rol TEXT CHECK(rol IN ('master', 'vendedor')) NOT NULL
    );
    """)
    # Insertar el usuario administrador por defecto si la tabla está vacía
    cursor.execute("INSERT OR IGNORE INTO usuarios (username, password, rol) VALUES ('admin', 'admin123', 'master');")

    # Tabla: Inventario Individual por Vendedor (Para el control de vasos asignados)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS inventario_vendedores (
        usuario_id INTEGER NOT NULL,
        producto_id INTEGER NOT NULL,
        stock_asignado INTEGER DEFAULT 0,
        PRIMARY KEY (usuario_id, producto_id),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE,
        FOREIGN KEY (producto_id) REFERENCES productos(id) ON DELETE CASCADE
    );
    """)



    # Tabla: Productos (Inventario)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY ,
        nombre TEXT NOT NULL,
        precio_compra REAL NOT NULL,
        precio_venta REAL NOT NULL,
        stock INTEGER DEFAULT 0,
        stock_minimo INTEGER DEFAULT 5
    );
    """)
    
    # Tabla: Ventas (Cabecera)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ventas (
        id INTEGER PRIMARY KEY ,
        fecha_hora TEXT DEFAULT (datetime('now', 'localtime')),
        total REAL NOT NULL,
        metodo_pago TEXT NOT NULL
    );
    """)
    
    # Tabla: Detalles de Ventas
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS detalles_ventas (
        id INTEGER PRIMARY KEY ,
        venta_id INTEGER NOT NULL,
        producto_id INTEGER NOT NULL,
        cantidad INTEGER NOT NULL,
        precio_unitario REAL NOT NULL,
        FOREIGN KEY (venta_id) REFERENCES ventas(id) ON DELETE CASCADE,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
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



def db_asignar_producto_a_vendedor(vendedor_name, producto_id, cantidad_a_asignar):
    """Saca stock del inventario global y se lo asigna a un vendedor específico."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    try:
        vendedor_name = str(vendedor_name).upper()
        cantidad_a_asignar = int(cantidad_a_asignar)
        
        # 1. Obtener el ID del usuario/vendedor basado en su nombre
        cursor.execute("SELECT id FROM usuarios WHERE UPPER(username) = ?", (vendedor_name,))
        usuario = cursor.fetchone()
        if not usuario:
            print(f"Error: El vendedor {vendedor_name} no existe en la tabla usuarios.")
            return False
        usuario_id = usuario[0]

        # 2. Verificar si hay suficiente stock global en el inventario general
        cursor.execute("SELECT stock FROM productos WHERE id = ?", (producto_id,))
        prod = cursor.fetchone()
        if not prod or prod[0] < cantidad_a_asignar:
            print("Error: No hay suficiente stock en el inventario global.")
            return False

        # 3. Restar del inventario global (productos)
        cursor.execute("UPDATE productos SET stock = stock - ? WHERE id = ?", (cantidad_a_asignar, producto_id))

        # 4. Sumar al inventario individual del vendedor (Si no existe el registro, se crea con INSERT OR IGNORE)
        cursor.execute("""
            INSERT OR IGNORE INTO inventario_vendedores (usuario_id, producto_id, stock_asignado)
            VALUES (?, ?, 0)
        """, (usuario_id, producto_id))
        
        cursor.execute("""
            UPDATE inventario_vendedores 
            SET stock_asignado = stock_asignado + ? 
            WHERE usuario_id = ? AND producto_id = ?
        """, (cantidad_a_asignar, usuario_id, producto_id))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error al asignar vasos: {e}")
        return False
    finally:
        conn.close()

def db_obtener_stock_actual_vendedores():
    """Devuelve una lista con los vasos que tiene actualmente cada vendedor en su puesto."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    datos = []
    try:
        cursor.execute("""
            SELECT u.username, p.nombre, iv.stock_asignado 
            FROM inventario_vendedores iv
            JOIN usuarios u ON iv.usuario_id = u.id
            JOIN productos p ON iv.producto_id = p.id
            ORDER BY u.username ASC, p.id ASC
        """)
        datos = cursor.fetchall()
    except Exception as e:
        print(f"Error al obtener inventario de vendedores: {e}")
    finally:
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
    """Suma las ventas en $ por cada vendedor filtrado por un día específico (YYYY-MM-DD)."""
    
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    reporte = {}
    try:
        query = "SELECT vendedor, SUM(total) FROM ventas"
        params = []
        
        # Filtro estricto por el día seleccionado (usando la columna que ya tienes validada)
        if fecha_seleccionada:
            query += " WHERE DATE(fecha_hora) = DATE(?)"
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
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    
    # 1. CONSULTA DE VENTAS
    if fecha_inicio and fecha_fin:
        # SQLite guarda "YYYY-MM-DD HH:MM:SS" en ventas, usamos date() para extraer solo la fecha
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
    
    # 3. ALERTA DE STOCK (Mantenido global e independiente del tiempo)
    cursor.execute("SELECT COUNT(*) FROM productos WHERE stock <= stock_minimo;")
    res_a = cursor.fetchone()
    p_alerta = res_a[0] if res_a and res_a[0] is not None else 0
    
    conn.close()
    return total_ventas, total_egresos, (total_ventas - total_egresos), p_alerta

def db_insertar_producto(nombre, p_compra, p_venta, stock, stock_min):
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    
    # Verificar si el producto ya existe (ignorando mayúsculas/minúsculas)
    cursor.execute("SELECT id, stock FROM productos WHERE LOWER(nombre) = LOWER(?)", (nombre,))
    existente = cursor.fetchone()
    
    if existente:
        # Si ya existe, actualiza el stock sumando el nuevo, y actualiza los precios
        id_p, stock_actual = existente
        nuevo_stock = stock_actual + stock
        cursor.execute("""
            UPDATE productos 
            SET precio_compra = ?, precio_venta = ?, stock = ?, stock_minimo = ? 
            WHERE id = ?
        """, (p_compra, p_venta, nuevo_stock, stock_min, id_p))
    else:
        # Si no existe, lo crea desde cero
        cursor.execute("""
            INSERT INTO productos (nombre, precio_compra, precio_venta, stock, stock_minimo) 
            VALUES (?, ?, ?, ?, ?)
        """, (nombre, p_compra, p_venta, stock, stock_min))
        
    conn.commit()
    conn.close()


def db_actualizar_producto(id_p, nombre, p_compra, p_venta, stock, stock_min):
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE productos 
        SET nombre = ?, precio_compra = ?, precio_venta = ?, stock = ?, stock_minimo = ? 
        WHERE id = ?
    """, (nombre, p_compra, p_venta, stock, stock_min, id_p))
    conn.commit()
    conn.close()

def db_eliminar_producto(id_p):
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM productos WHERE id = ?", (id_p,))
    conn.commit()
    conn.close()

def db_obtener_productos():
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, precio_compra, precio_venta, stock, stock_minimo FROM productos ORDER BY nombre ASC")
    datos = cursor.fetchall()
    conn.close()
    return datos



def db_buscar_producto_venta(termino):
    conn = inicializar_base_datos()

    
def db_registrar_venta_completa(carrito, total, metodo, vendedor_name, referencia_pm=None):
    """Registra la venta y descuenta los vasos directamente de la asignación del vendedor."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    try:
        # --- ASEGURAR COLUMNAS (Formato correcto para Turso/libsql) ---
        try: 
            cursor.execute("ALTER TABLE ventas ADD COLUMN vendedor TEXT DEFAULT 'ADMIN';")
            conn.commit()
        except Exception as e: 
            if "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
                raise e

        try: 
            cursor.execute("ALTER TABLE ventas ADD COLUMN referencia_pm TEXT DEFAULT '';")
            conn.commit()
        except Exception as e: 
            if "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
                raise e

        try: 
            cursor.execute("ALTER TABLE ventas ADD COLUMN conciliado INTEGER DEFAULT 0;")
            conn.commit()
        except Exception as e: 
            if "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
                raise e
        # ---------------------------------------------------------------

        vendedor_limpio = str(vendedor_name).upper()

        # 1. Insertar la cabecera de la venta
        cursor.execute(
            "INSERT INTO ventas (total, metodo_pago, vendedor, referencia_pm, conciliado) VALUES (?, ?, ?, ?, 0)", 
            (float(total), metodo, vendedor_limpio, str(referencia_pm or ""))
        )
        v_id = cursor.lastrowid
        
        # 2. Buscar el id del vendedor para poder descontar de su inventario asignado
        cursor.execute("SELECT id FROM usuarios WHERE UPPER(username) = ?", (vendedor_limpio,))
        usuario_row = cursor.fetchone()
        usuario_id = usuario_row[0] if usuario_row else None

        # 3. Procesar los artículos del carrito
        for item in carrito:
            id_p = int(item[0])
            precio_u = float(item[2])
            cantidad = int(item[3])

            # Insertar en el detalle de la venta
            cursor.execute("""
                INSERT INTO detalles_ventas (venta_id, producto_id, cantidad, precio_unitario) 
                VALUES (?, ?, ?, ?)
            """, (v_id, id_p, cantidad, precio_u))
            
            # DESCUENTO INTELIGENTE:
            if usuario_id:
                # Si el usuario tiene asignación en el puesto, se le resta a él
                cursor.execute("""
                    UPDATE inventario_vendedores 
                    SET stock_asignado = stock_asignado - ? 
                    WHERE usuario_id = ? AND producto_id = ?
                """, (cantidad, usuario_id, id_p))
            else:
                # Si vende un usuario sin asignar, descuenta del global
                cursor.execute("UPDATE productos SET stock = stock - ? WHERE id = ?", (cantidad, id_p))
            
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error en base de datos al vender: {e}")
        return False
    finally:
        conn.close()




def db_obtener_historial_ventas():
    """Trae las ventas de forma segura adaptándose al nombre real de la columna de fecha."""
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    datos = []
    try:
        # --- ASEGURAR COLUMNAS (Formato correcto para Turso/libsql) ---
        try: 
            cursor.execute("ALTER TABLE ventas ADD COLUMN referencia_pm TEXT DEFAULT '';")
            conn.commit()
        except Exception as e: 
            if "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
                raise e

        try: 
            cursor.execute("ALTER TABLE ventas ADD COLUMN conciliado INTEGER DEFAULT 0;")
            conn.commit()
        except Exception as e: 
            if "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
                raise e
        # ---------------------------------------------------------------
        
        # INTENTO 1: Probamos con 'fecha_hora'
        try:
            cursor.execute("SELECT id, fecha_hora, total, metodo_pago, vendedor, referencia_pm, conciliado FROM ventas ORDER BY id DESC")
            datos = cursor.fetchall()
        except Exception as e:
            # Si el error es porque no encuentra la columna 'fecha_hora', pasamos al INTENTO 2
            if "no such column" in str(e).lower() or "has no column" in str(e).lower():
                # INTENTO 2: Si falla por el nombre, probamos con 'fecha'
                cursor.execute("SELECT id, fecha, total, metodo_pago, vendedor, referencia_pm, conciliado FROM ventas ORDER BY id DESC")
                datos = cursor.fetchall()
            else:
                # Si fue otro tipo de error diferente al nombre de la columna, lo relanzamos
                raise e
            
    except Exception as e:
        print(f"Error definitivo en consulta de historial: {e}")
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
    conn = inicializar_base_datos()
    cursor = conn.cursor()
    # Vincula los detalles con la tabla de productos para traer el nombre
    cursor.execute("""
        SELECT p.nombre, dv.cantidad, dv.precio_unitario 
        FROM detalles_ventas dv
        JOIN productos p ON dv.producto_id = p.id
        WHERE dv.venta_id = ?
    """, (venta_id,))
    datos = cursor.fetchall()
    conn.close()
    return datos


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

    return ft.Column([
        ft.Text("📊 Tablero de Control / Finanzas", size=26, weight=ft.FontWeight.BOLD),
        ft.Text("Resumen analítico de las finanzas. Filtra por rango para auditar periodos específicos.", size=14, color=ft.Colors.GREY_700),
        ft.Divider(),
        
        # Nueva Fila de Filtros de Fecha
        ft.Container(
            content=ft.Row([
                ft.Text("Rango:", weight=ft.FontWeight.BOLD, size=14),
                btn_fecha_ini,
                ft.Text("al"),
                btn_fecha_fin,
                btn_filtrar,
                btn_limpiar
            ], alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
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

# --- PANTALLA 2: INVENTARIO CON DOBLE MONEDA (BLINDADO MATEMÁTICAMENTE) ---
def vista_inventario(page: ft.Page):
    page.floating_action_button = None
    page.update()

    producto_id_editar = None

    txt_nombre = ft.TextField(label="Producto", dense=True)
    txt_p_compra = ft.TextField(label="Costo ($)", dense=True, hint_text="Ej: 10.50") 
    txt_p_venta = ft.TextField(label="Venta ($)", dense=True, hint_text="Ej: 15.00")  
    txt_stock = ft.TextField(label="Stock", value="0", keyboard_type=ft.KeyboardType.NUMBER, dense=True)
    txt_stock_min = ft.TextField(label="Mín.", value="5", keyboard_type=ft.KeyboardType.NUMBER, dense=True)

    lista_productos_movil = ft.ListView(expand=True, spacing=5)

    def limpiar_campos():
        nonlocal producto_id_editar
        producto_id_editar = None
        txt_nombre.value = ""
        txt_p_compra.value = ""
        txt_p_venta.value = ""
        txt_stock.value = "0"
        txt_stock_min.value = "5"
        btn_guardar.content = ft.Text("Añadir")
        btn_guardar.icon = ft.Icons.ADD
        btn_guardar.bgcolor = ft.Colors.GREEN
        btn_cancelar.visible = False
        page.update()

    def actualizar():
        lista_productos_movil.controls.clear()
        tasa = float(db_obtener_tasa_dolar())
        
        for prod in db_obtener_productos():
            id_p, nombre, p_compra, p_venta, stock, stock_min = prod
            en_alerta = int(stock) <= int(stock_min)
            
            precio_dolar = float(p_venta)
            p_venta_bs = precio_dolar * tasa
            col_stock = ft.Colors.RED_700 if en_alerta else ft.Colors.GREEN_700
            
            id_actual = id_p
            nombre_actual = nombre
            pc_actual = float(p_compra)
            pv_actual = precio_dolar
            st_actual = int(stock)
            sm_actual = int(stock_min)

            lista_productos_movil.controls.append(
                ft.Card(
                    content=ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.INVENTORY_2, color=col_stock, size=24),
                            ft.Column([
                                ft.Text(str(nombre), weight=ft.FontWeight.BOLD, size=14),
                                ft.Text(f"Costo: ${pc_actual:.2f} | Mín: {sm_actual}", size=11, color=ft.Colors.GREY_600)
                            ], spacing=2, expand=True),
                            ft.Column([
                                ft.Text(f"${pv_actual:.2f}", weight=ft.FontWeight.BOLD, size=14, color=ft.Colors.BLUE_900),
                                ft.Text(f"{p_venta_bs:.1f} Bs.", size=11, color=ft.Colors.GREY_700)
                            ], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=2),
                            ft.Row([
                                ft.IconButton(
                                    icon=ft.Icons.EDIT,
                                    icon_color=ft.Colors.BLUE_500,
                                    icon_size=18,
                                    on_click=lambda e, idx=id_actual, n=nombre_actual, pc=pc_actual, pv=pv_actual, s=st_actual, sm=sm_actual: preparar_edicion(idx, n, pc, pv, s, sm)
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.DELETE,
                                    icon_color=ft.Colors.RED_500,
                                    icon_size=18,
                                    on_click=lambda e, idx=id_actual, n=nombre_actual: abrir_confirmacion_borrado(idx, n)
                                )
                            ], spacing=0, alignment=ft.MainAxisAlignment.END),
                            ft.Container(
                                content=ft.Text(str(stock), color=ft.Colors.WHITE, size=11, weight=ft.FontWeight.BOLD),
                                bgcolor=col_stock,
                                padding=ft.Padding(8, 4, 8, 4), 
                                border_radius=12,
                                margin=ft.Margin(5, 0, 0, 0)
                            )
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        padding=10
                    ),
                    elevation=1
                )
            )
        page.update()

    def preparar_edicion(id_p, nombre, p_compra, p_venta, stock, stock_min):
        nonlocal producto_id_editar
        producto_id_editar = id_p
        txt_nombre.value = str(nombre)
        txt_p_compra.value = str(p_compra)
        txt_p_venta.value = str(p_venta)
        txt_stock.value = str(stock)
        txt_stock_min.value = str(stock_min)
        btn_guardar.content = ft.Text("Actualizar")
        btn_guardar.icon = ft.Icons.SAVE
        btn_guardar.bgcolor = ft.Colors.BLUE_800
        btn_cancelar.visible = True
        page.update()

    def abrir_confirmacion_borrado(id_p, nombre_p):
        def confirmar(e):
            db_eliminar_producto(id_p)
            dialogo.open = False
            page.update()
            actualizar()

        def cancelar(e):
            dialogo.open = False
            page.update()

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Confirmar Eliminación"),
            content=ft.Text(f"¿De verdad deseas borrar '{nombre_p}' del inventario?"),
            actions=[
                ft.TextButton("Cancelar", on_click=cancelar),
                ft.ElevatedButton(content=ft.Text("Eliminar"), bgcolor=ft.Colors.RED, color=ft.Colors.WHITE, on_click=confirmar),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.overlay.append(dialogo)
        dialogo.open = True
        page.update()

    def guardar_click(e):
        if not all([txt_nombre.value, txt_p_compra.value, txt_p_venta.value, txt_stock.value, txt_stock_min.value]): 
            return
        try:
            nom = txt_nombre.value.strip()
            costo_texto = txt_p_compra.value.strip().replace(",", ".")
            venta_texto = txt_p_venta.value.strip().replace(",", ".")
            pc = float(costo_texto)
            pv = float(venta_texto)
            st = int(txt_stock.value.strip())
            sm = int(txt_stock_min.value.strip())

            if producto_id_editar is not None:
                db_actualizar_producto(producto_id_editar, nom, pc, pv, st, sm)
            else:
                db_insertar_producto(nom, pc, pv, st, sm)

            limpiar_campos()
            actualizar()
        except ValueError: 
            pass

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

    page.floating_action_button = ft.FloatingActionButton(
        icon=ft.Icons.LOCAL_SHIPPING,
        bgcolor=ft.Colors.GREEN_700,
        tooltip="Despachar Vasos a Puestos",
        on_click=lambda e: abrir_modal_puestos_separado(page, al_cerrar=actualizar)
    )
    page.update()

    return ft.Column([
        ft.Text("📦 Inventario General de Vasos", size=20, weight=ft.FontWeight.BOLD),
        ft.ResponsiveRow([
            ft.Container(txt_nombre, col=12),
            ft.Container(txt_p_compra, col=3),
            ft.Container(txt_p_venta, col=3),
            ft.Container(txt_stock, col=3),
            ft.Container(txt_stock_min, col=3),
        ], run_spacing=5),
        ft.Row([btn_cancelar, btn_guardar], alignment=ft.MainAxisAlignment.END, spacing=10),
        ft.Divider(height=10),
        lista_productos_movil
    ], expand=True)
def abrir_modal_puestos_separado(page: ft.Page, al_cerrar=None):
    lista_existencias_vendedores = ft.ListView(spacing=5, height=180, scroll=ft.ScrollMode.AUTO)
    dropdown_vendedor = ft.Dropdown(label="Vendedor", expand=True)
    dropdown_vaso = ft.Dropdown(label="Vaso / Producto", expand=True)
    txt_cantidad = ft.TextField(label="Cant.", value="0", width=80, text_align=ft.TextAlign.RIGHT, keyboard_type=ft.KeyboardType.NUMBER)

    def cargar_datos_modal():
        dropdown_vendedor.options.clear()
        dropdown_vaso.options.clear()
        lista_existencias_vendedores.controls.clear()

        try:
            for u in db_obtener_todos_usuarios():
                if u[2] == "vendedor":
                    dropdown_vendedor.options.append(ft.DropdownOption(key=str(u[1]), text=str(u[1]).upper()))
        except Exception as ex_u:
            print(ex_u)

        try:
            for prod in db_obtener_productos():
                dropdown_vaso.options.append(ft.DropdownOption(key=str(prod[0]), text=f"{prod[1]} (Disp: {prod[4]})"))
        except Exception as ex_p:
            print(ex_p)

        try:
            existencias = db_obtener_stock_actual_vendedores()
            if not existencias:
                lista_existencias_vendedores.controls.append(ft.Text("No hay vasos asignados a puestos.", size=12, color=ft.Colors.GREY_500, italic=True))
            else:
                for ex in existencias:
                    vendedor_puesto, nombre_vaso, cantidad_asignada = ex
                    color_alerta = ft.Colors.ORANGE_700 if cantidad_asignada <= 5 else ft.Colors.BLUE_GREY_700
                    if cantidad_asignada <= 0:
                        color_alerta = ft.Colors.RED_700
                    lista_existencias_vendedores.controls.append(
                        ft.Row([
                            ft.Row([
                                ft.Icon(ft.Icons.HOME, color=ft.Colors.BLUE_700, size=16),
                                ft.Text(f"{str(vendedor_puesto).upper()} - {nombre_vaso}", size=12)
                            ], spacing=5),
                            ft.Text(f"{cantidad_asignada} und.", weight=ft.FontWeight.BOLD, size=12, color=color_alerta)
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                    )
        except Exception as ex_t:
            print(ex_t)
        page.update()

    def ejecutar_traslado_click(ev):
        if not dropdown_vendedor.value or not dropdown_vaso.value:
            return
        try:
            cantidad = int(txt_cantidad.value)
            if cantidad <= 0:
                return
        except ValueError:
            return

        exito = db_asignar_producto_a_vendedor(
            vendedor_name=dropdown_vendedor.value,
            producto_id=int(dropdown_vaso.value),
            cantidad_a_asignar=cantidad
        )

        if exito:
            txt_cantidad.value = "0"
            cargar_datos_modal()

    cargar_datos_modal()

    dialogo_asignacion = ft.AlertDialog(
        title=ft.Row([
            ft.Icon(ft.Icons.LOCAL_SHIPPING, color=ft.Colors.GREEN_700),
            ft.Text("Despachar a Puestos", size=16, weight=ft.FontWeight.BOLD)
        ], spacing=8),
        content=ft.Column([
            ft.Text("Transferir desde Almacén:", size=11, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_ACCENT_700),
            ft.Row([dropdown_vendedor, txt_cantidad], spacing=5),
            dropdown_vaso,
            ft.ElevatedButton(
                content=ft.Text("Confirmar Entrega"),
                icon=ft.Icons.ADD,
                bgcolor=ft.Colors.GREEN_700,
                color=ft.Colors.WHITE,
                width=300,
                on_click=ejecutar_traslado_click
            ),
            ft.Divider(height=15),
            ft.Text("Vasos activos en la calle:", size=11, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_700),
            lista_existencias_vendedores
        ], tight=True, width=320, spacing=8),
        actions=[
            ft.TextButton("Cerrar", on_click=lambda _: cerrar_modal())
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def cerrar_modal():
        dialogo_asignacion.open = False
        page.update()
        if al_cerrar:
            al_cerrar()

    page.overlay.append(dialogo_asignacion)
    dialogo_asignacion.open = True
    page.update()




# --- PANTALLA 3: PUNTO DE VENTA CON DOBLE MONEDA ---
carrito_chicha = []

def vista_ventas(page: ft.Page):
    # ESTO BORRA EL BOTÓN AUTOMÁTICAMENTE AL ENTRAR A ESTA PESTAÑA
    page.floating_action_button = None
    page.update()


    cuadricula_productos = ft.GridView(
        expand=False,
        height=180,              
        runs_count=3,            
        max_extent=160,          
        spacing=8,
        run_spacing=8,
    )
    
    lista_carrito = ft.ListView(expand=True, spacing=4) 
    lbl_total = ft.Text("Total: $0.00 (0.00 Bs.)", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700)
    
    dd_metodo = ft.Dropdown(
        label="Pago", 
        options=[ft.dropdown.Option("EFECTIVO"), ft.dropdown.Option("PUNTO"), ft.dropdown.Option("PAGO MOVIL")], 
        value="EFECTIVO", 
        dense=True,
        width=150 
    )

    def render_carrito():
        lista_carrito.controls.clear()
        total_acumulado = 0.0
        tasa = float(db_obtener_tasa_dolar())
        
        for idx, art in enumerate(carrito_chicha):
            precio_individual = float(art[2])
            cantidad_unidades = int(art[3])
            subtotal_renglon = precio_individual * cantidad_unidades
            total_acumulado += subtotal_renglon
            
            id_actual = idx

            lista_carrito.controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Text(f"{cantidad_unidades}x", weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800),
                        ft.Text(str(art[1]), expand=True, size=16), 
                        ft.Text(f"${subtotal_renglon:.2f}", size=16, weight=ft.FontWeight.BOLD),
                        ft.IconButton(
                            icon=ft.Icons.REMOVE_CIRCLE_OUTLINE, 
                            icon_color=ft.Colors.RED_400, 
                            icon_size=18, 
                            on_click=lambda e, i=id_actual: quitar(i)
                        )
                    ]),
                    padding=6, bgcolor=ft.Colors.GREY_50, border_radius=6
                )
            )
        total_bs = total_acumulado * tasa
        lbl_total.value = f"Total: ${total_acumulado:.2f} ({total_bs:.1f} Bs.)"
        page.update()

    def quitar(index):
        carrito_chicha.pop(index)
        render_carrito()

    def agregar_al_carrito(prod):
        for art in carrito_chicha:
            if int(art[0]) == int(prod[0]):
                if int(art[3]) < int(prod[3]): 
                    art[3] += 1
                    render_carrito()
                else:
                    page.open(ft.SnackBar(ft.Text(f"No hay más stock disponible de {prod[1]}"), bgcolor="orange"))
                return
        
        carrito_chicha.append([int(prod[0]), str(prod[1]), float(prod[2]), 1])
        render_carrito()

    def cargar_productos_fijos():
        cuadricula_productos.controls.clear()
        tasa = float(db_obtener_tasa_dolar())
        
        productos = db_obtener_productos()
        for p in productos:
            id_p, nombre, p_compra, p_venta, stock, stock_min = p
            precio_bs = float(p_venta) * tasa
            
            prod_data = (id_p, nombre, p_venta, stock)
            
            cuadricula_productos.controls.append(
                ft.Card(
                    content=ft.Container(
                        on_click=lambda e, pd=prod_data: agregar_al_carrito(pd),
                        border_radius=8,
                        padding=6,
                        content=ft.Column([
                            ft.Icon(ft.Icons.LOCAL_DRINK, color=ft.Colors.BLUE_500, size=24),
                            ft.Text(nombre, weight=ft.FontWeight.BOLD, size=12, text_align=ft.TextAlign.CENTER, max_lines=1),
                            ft.Text(f"${float(p_venta):.2f}", size=11, color=ft.Colors.GREEN_700, weight=ft.FontWeight.W_600),
                            ft.Text(f"{precio_bs:.1f} Bs.", size=9, color=ft.Colors.GREY_600),
                        ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2)
                    ),
                    elevation=2
                )
            )
        page.update()

    def ejecutar_registro(referencia):
        if not carrito_chicha: return
        total_venta = sum(float(art[2]) * int(art[3]) for art in carrito_chicha)
        
        vendedor_actual = "ADMIN"
        try:
            for control in page.controls:
                if hasattr(control, "controls"):
                    for sub_control in control.controls:
                        if hasattr(sub_control, "content") and hasattr(sub_control.content, "controls"):
                            for el in sub_control.content.controls:
                                if isinstance(el, ft.Row) and len(el.controls) > 0:
                                    texto_header = str(el.controls[0].value)
                                    if "Vendedor:" in texto_header:
                                        vendedor_actual = texto_header.replace("👤 Vendedor:", "").strip()
                                        break
        except Exception:
            vendedor_actual = "ADMIN"

        if db_registrar_venta_completa(carrito_chicha, total_venta, dd_metodo.value, vendedor_actual, referencia):
            carrito_chicha.clear()
            render_carrito()
            lbl_total.value = "¡Venta Registrada!"
            cargar_productos_fijos() 
            page.update()

    def procesar_venta(e): 
        if not carrito_chicha: return
        
        if dd_metodo.value == "PAGO MOVIL":
            txt_referencia = ft.TextField(label="Número de Referencia", keyboard_type=ft.KeyboardType.NUMBER, autofocus=True)
            
            def guardar_con_referencia(e_dialog):
                ref = txt_referencia.value.strip()
                if not ref:
                    page.open(ft.SnackBar(ft.Text("⚠️ Debes ingresar un número de referencia"), bgcolor="orange"))
                    return
                
                page.overlay.remove(dialogo_pm)
                page.update()
                ejecutar_registro(ref)

            dialogo_pm = ft.AlertDialog(
                title=ft.Text("📱 Confirmar Pago Móvil"),
                content=ft.Column([
                    ft.Text("Ingrese el número de comprobante bancario para continuar:"),
                    txt_referencia
                ], tight=True, spacing=10),
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda _: [page.overlay.remove(dialogo_pm), page.update()]),
                    ft.ElevatedButton("Guardar Venta", bgcolor=ft.Colors.GREEN_700, color=ft.Colors.WHITE, on_click=guardar_con_referencia)
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page.overlay.append(dialogo_pm)
            dialogo_pm.open = True
            page.update()
        else:
            ejecutar_registro(None)

    btn_pagar = ft.ElevatedButton(
        "Cobrar Venta", 
        icon=ft.Icons.CHECK, 
        bgcolor=ft.Colors.GREEN_700, 
        color=ft.Colors.WHITE, 
        on_click=procesar_venta, 
        expand=True
    )

    cargar_productos_fijos()
    render_carrito()

    return ft.Column([
        ft.Text("🥤 LA RICA CHICHA DE CARACAS ", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_800),
        cuadricula_productos,
        ft.Text("🛒 Carrito Actual:", size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_700),
        lista_carrito,
        ft.Divider(height=5),
        ft.Row([
            dd_metodo,
            lbl_total
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Row([btn_pagar])
    ], expand=True)



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


# --- PANTALLA 5: HISTORIAL DE VENTAS COMPACTO CON CHECKBOX UNIVERSAL ---

def vista_historial_ventas(page: ft.Page):
    lista_historial = ft.ListView(spacing=5, height=500, auto_scroll=False)

    def generar_tarjeta_venta(id_venta, fecha_venta, total_venta, metodo_pago, tasa_dolar, vendedor_ticket, referencia_pm, conciliado_inicial):
        # 1. Aseguramos la multiplicación de la tasa sin romper nada
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
        
        # Tratamos de cargar los productos, si falla ponemos un aviso simple
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
                            ft.Text(f"• {cantidad}x {nombre_p}", size=12, color=ft.Colors.BLUE_GREY_700, expand=True),
                            ft.Text(f"${sub:.2f}", size=12, weight=ft.FontWeight.W_500, color=ft.Colors.BLUE_GREY_900)
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

        # 2. USAREMOS UN CHECKBOX NATIVO QUE ES 100% SEGURO Y NO SE ROMPE
        chk_banco = ft.Checkbox(
            value=True if conciliado_inicial == 1 else False,
            fill_color=ft.Colors.GREEN_600,
        )

        def cambiar_estado_checkbox(e):
            # Si el usuario quiere DESTILDAR (quitar el Check)
            if chk_banco.value == False:
                def confirmar_destildar(ev):
                    db_actualizar_conciliacion(id_venta, 0)
                    chk_banco.value = False
                    dialogo_alerta.open = False
                    page.overlay.remove(dialogo_alerta)
                    page.update()

                def cancelar_destildar(ev):
                    chk_banco.value = True  # Le devolvemos el check si cancela
                    dialogo_alerta.open = False
                    page.overlay.remove(dialogo_alerta)
                    page.update()

                dialogo_alerta = ft.AlertDialog(
                    title=ft.Text("⚠️ Advertencia de Control"),
                    content=ft.Text(f"¿Deseas quitar la verificación del Folio {folio_str}? Volverá a marcarse como pendiente en el banco."),
                    actions=[
                        ft.TextButton("Cancelar", on_click=cancelar_destildar),
                        ft.ElevatedButton("Sí, quitar", bgcolor=ft.Colors.RED_600, color=ft.Colors.WHITE, on_click=confirmar_destildar)
                    ],
                    actions_alignment=ft.MainAxisAlignment.END
                )
                page.overlay.append(dialogo_alerta)
                dialogo_alerta.open = True
                page.update()
            else:
                # Si lo quiere TILDAR, se guarda directo en la base de datos
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
                        chk_banco,  # Tu control de banco seguro aquí
                        ft.Icon(ft.Icons.RECEIPT, color=ft.Colors.BLUE_GREY, size=22),
                        ft.Column([
                            ft.Text(f"Folio {folio_str} - {texto_metodo}", weight=ft.FontWeight.BOLD, size=13),
                            ft.Text(f"Atendido por: {vendedor_ticket}", size=11, color=ft.Colors.BLUE_GREY_500, weight=ft.FontWeight.W_500),
                            ft.Text(str(fecha_venta), size=10, color=ft.Colors.GREY_600)
                        ], spacing=1, expand=True),
                        ft.Column([
                            ft.Text(f"${total_float:.2f}", weight=ft.FontWeight.BOLD, size=13, color=ft.Colors.GREEN_700),
                            ft.Text(f"{total_bs:.1f} Bs.", size=11, color=ft.Colors.BLUE_900)
                        ], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=1),
                        ft.IconButton(
                            icon=ft.Icons.INFO, 
                            icon_size=20, 
                            icon_color=ft.Colors.BLUE,
                            on_click=conmutar_detalle
                        )
                    ]),
                    contenedor_detalle
                ]),
                padding=10
            )
        )

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
                id_v = v[0]
                fecha = v[1]
                
                # Búsqueda segura del total numérico en la tupla
                total = 0.0
                for item in v:
                    if isinstance(item, (int, float)):
                        total = float(item)
                        break
                    try:
                        if "." in str(item) and float(item):
                            total = float(item)
                            break
                    except ValueError:
                        continue
                if total == 0.0:
                    try: total = float(v[2])
                    except Exception: total = 0.0

                metodo = v[3]
                vendedor = v[4] if len(v) > 4 and v[4] else "ADMIN"
                referencia = v[5] if len(v) > 5 and v[5] else ""
                
                try:
                    conciliado = int(v[6]) if len(v) > 6 and v[6] is not None else 0
                except Exception:
                    conciliado = 0
                
                tarjeta = generar_tarjeta_venta(id_v, fecha, total, metodo, tasa, vendedor, referencia, conciliado)
                lista_historial.controls.append(tarjeta)
            except Exception as e_fila:
                print(f"Fila ignorada por error menor: {e_fila}")
                continue
            
        page.update()

    # =========================================================================
    #  BLOQUE DE FILTRADO CORREGIDO Y COMPACTO (REEMPLAZAR AL FINAL)
    # =========================================================================
    
    # Creamos un contenedor de texto dedicado con formato limpio
    texto_boton_fecha = ft.Text("Seleccionar Día", size=13, weight=ft.FontWeight.W_500)

    # Botón principal utilizando page.show_dialog() nativo de Flet 0.86
    btn_fecha_texto = ft.ElevatedButton(
        content=texto_boton_fecha,
        icon="calendar_month",
        on_click=lambda _: page.show_dialog(picker_historial)
    )

    def al_cambiar_fecha(e):
        if picker_historial.value:
            fecha_elegida = picker_historial.value.date().strftime("%Y-%m-%d")
            # Actualizamos el texto en pantalla de forma segura
            texto_boton_fecha.value = f"Día: {picker_historial.value.date().strftime('%d/%m/%Y')}"
            texto_boton_fecha.update()
            btn_reset.visible = True
            btn_reset.update()
            
            # Filtramos visualmente recorriendo tus tarjetas de la lista sin tocar la base de datos
            for tarjeta in lista_historial.controls[:]:
                try:
                    # Accedemos de forma segura a la tercera fila de textos donde pusiste str(fecha_venta)
                    # Tarjeta -> Container -> Column -> Row -> Column (índice 2) -> Text (índice 2, la fecha)
                    componentes_fila = tarjeta.content.content.controls[0].controls[2].controls
                    texto_fecha_tarjeta = componentes_fila[2].value
                    
                    # Si el string de la fecha elegida está en el texto del registro, se queda visible
                    tarjeta.visible = (fecha_elegida in str(texto_fecha_tarjeta))
                except Exception:
                    continue
            lista_historial.update()

    def restablecer_todo(e):
        texto_boton_fecha.value = "Seleccionar Día"
        texto_boton_fecha.update()
        btn_reset.visible = False
        btn_reset.update()
        # Restauramos la visibilidad de todo tu historial cargado
        for tarjeta in lista_historial.controls:
            tarjeta.visible = True
        lista_historial.update()

    # SOLUCIÓN AL BANNER ROJO: Le agregamos un icono base por defecto para que Flet no proteste al iniciar
        # SOLUCIÓN AL BANNER ROJO: Le agregamos un icono base por defecto para que Flet no proteste al iniciar
    btn_reset = ft.IconButton(
        icon="close",
        icon_color="red",
        tooltip="Quitar Filtro",
        visible=False,
        on_click=restablecer_todo
    )

    # =========================================================================
    # NUEVA LÓGICA: VENTANA FLOTANTE DE REPORTES (SUMA EN $)
    # =========================================================================
    def mostrar_reporte_vendedores_click(e):
        reporte_sumas = {}
        
        # Recorremos de forma segura las tarjetas que se encuentran visibles en pantalla
        for tarjeta in lista_historial.controls:
            try:
                # Si aplicaste el filtro por día, ignoramos las tarjetas ocultas
                if not tarjeta.visible:
                    continue
                
                # Mapeo seguro de la estructura interna de tu tarjeta:
                # Tarjeta -> Container -> Column -> Row
                fila_principal = tarjeta.content.content.controls[0]
                
                # Fila -> Column (índice 2) -> Text (índice 1, es el de 'Atendido por: VENDEDOR')
                texto_vendedor_crudo = fila_principal.controls[2].controls[1].value
                # Limpiamos el prefijo 'Atendido por: ' para quedarnos solo con el nombre en MAYÚSCULAS
                vendedor = str(texto_vendedor_crudo).replace("Atendido por:", "").strip().upper()
                if not vendedor:
                    vendedor = "ADMIN"
                
                # Fila -> Column (índice 3, los precios) -> Text (índice 0, el precio en '$XX.XX')
                texto_precio_crudo = fila_principal.controls[3].controls[0].value
                # Limpiamos el caracter '$' y convertimos a número flotante
                monto_usd = float(str(texto_precio_crudo).replace("$", "").strip())
                
                # Acumulamos la sumatoria en dólares ($)
                if vendedor in reporte_sumas:
                    reporte_sumas[vendedor] += monto_usd
                else:
                    reporte_sumas[vendedor] = monto_usd
            except Exception as err_lectura:
                # Si una tarjeta tiene estructura corrupta o está vacía se ignora elegantemente
                continue

                # Creamos las filas visuales dinámicas para nuestro modal
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
                        # SOLUCIÓN AL ERROR: Sintaxis compatible con todas las versiones de Flet
                        padding=ft.Padding(10, 6, 10, 6),
                        border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.GREY_200))
                    )
                )


        # Configuramos y construimos el AlertDialog flotante
        dialogo_reporte = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.ASSESSMENT_ROUNDED, color=ft.Colors.GREEN_700),
                ft.Text("Sumatoria de Ventas ($)", size=16, weight=ft.FontWeight.BOLD)
            ], spacing=10),
            content=ft.Column(
                controls=filas_vendedores_ui,
                tight=True,
                width=320,
                scroll=ft.ScrollMode.AUTO
            ),
            actions=[
                ft.TextButton("Cerrar", on_click=lambda _: cerrar_reporte_modal(dialogo_reporte))
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        page.overlay.append(dialogo_reporte)
        dialogo_reporte.open = True
        page.update()

    def cerrar_reporte_modal(dialogo):
        dialogo.open = False
        page.update()
    # =========================================================================

    # Botón tipo Icono para abrir el reporte sumado en $
    btn_reporte = ft.IconButton(
        icon=ft.Icons.BAR_CHART_ROUNDED,
        icon_color=ft.Colors.GREEN_700,
        tooltip="Ver totales por vendedor",
        on_click=mostrar_reporte_vendedores_click
    )

    import datetime
    picker_historial = ft.DatePicker(
        on_change=al_cambiar_fecha,
        first_date=datetime.datetime(2024, 1, 1),
        last_date=datetime.datetime(2030, 12, 31)
    )
    page.overlay.append(picker_historial)

    # Retorno exacto adaptado con el nuevo layout e icono integrado
    try:
        cargar_historial()
        return ft.Column([
            ft.Row([
                ft.Text("📜 Historial de Ventas Y Conciliación Bancaria", size=20, weight=ft.FontWeight.BOLD, expand=True),
                ft.Row([
                    btn_reporte,       # <-- Tu nuevo botón de reporte agregado
                    btn_fecha_texto, 
                    btn_reset
                ], alignment=ft.MainAxisAlignment.END, spacing=5)
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
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
        
        # TARJETA 2: ACCESO GRÁFICO AL PERSONAL (NUEVO)
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
                        on_click=lambda e: abrir_modal_usuarios(page) # Llama a la ventana flotante
                    )
                ]),
                padding=15
            ),
            elevation=2
        )
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

    inicializar_base_datos()

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
                ft.Icon(ft.Icons.LOCAL_DRINK_ROUNDED, size=50, color=ft.Colors.BLUE_800),
                ft.Text("CELER APP", size=24, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_900),
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
