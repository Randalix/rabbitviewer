import os
import time
import hashlib
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple, Optional # Importiere Tuple und Optional

# Pfad anpassen, damit die Plugins gefunden werden
# Dies ist notwendig, wenn das Benchmark-Skript nicht im selben Verzeichnis wie die Plugins liegt
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from plugins.cr3_plugin import CR3Plugin
from plugins.base_plugin import BasePlugin # Nur für den Fall, dass BasePlugin-Methoden direkt getestet werden

# Konfigurieren des Loggings, um die Ausgaben des Plugins zu sehen
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# --- KONFIGURATION ---
# ERSETZEN SIE DIES DURCH DEN TATSÄCHLICHEN PFAD ZU EINER IHRER CR3-DATEIEN
TEST_CR3_FILE = "/Volumes/storage/pictures/2025/2025_02_09-Valentina/_MG_2718.CR3" 
# Verzeichnis für Cache-Dateien der Benchmarks
BENCHMARK_CACHE_DIR = "benchmark_cache"
# Anzahl der Wiederholungen für den Benchmark
NUM_RUNS = 10
# Anzahl der parallelen Prozesse/Threads für den Parallel-Benchmark
NUM_PARALLEL_PROCESSES = 4 # Beispielwert, anpassen je nach CPU-Kernen/I/O-Last

def calculate_md5(file_path: str) -> str:
    """Berechnet den MD5-Hash einer Datei."""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def run_single_process(plugin: BasePlugin, file_path: str, md5_hash: str, run_name: str) -> Tuple[bool, float, Optional[str], Optional[str]]:
    """Hilfsfunktion zum Ausführen eines einzelnen Bildverarbeitungsprozesses."""
    start_time = time.time()
    success, view_path, thumb_path = plugin.process_image(file_path, md5_hash)
    end_time = time.time()
    run_time = end_time - start_time
    if success:
        logger.info(f"{run_name} erfolgreich. Zeit: {run_time:.4f} Sekunden")
    else:
        logger.error(f"{run_name} fehlgeschlagen. Zeit: {run_time:.4f} Sekunden")
    return success, run_time, view_path, thumb_path

def run_benchmark():
    if not os.path.exists(TEST_CR3_FILE):
        logger.error(f"Testdatei nicht gefunden: {TEST_CR3_FILE}")
        logger.error("Bitte passen Sie TEST_CR3_FILE in benchmarks/plugin_benchmarks.py an.")
        return

    # Sicherstellen, dass das Cache-Verzeichnis existiert und leer ist
    if os.path.exists(BENCHMARK_CACHE_DIR):
        import shutil
        shutil.rmtree(BENCHMARK_CACHE_DIR)
    os.makedirs(BENCHMARK_CACHE_DIR)

    logger.info(f"Starte Benchmark für CR3Plugin mit Datei: {TEST_CR3_FILE}")
    logger.info(f"Cache-Verzeichnis: {BENCHMARK_CACHE_DIR}")
    logger.info(f"Anzahl der Durchläufe (sequenziell): {NUM_RUNS}")
    logger.info(f"Anzahl der parallelen Prozesse (simuliert): {NUM_PARALLEL_PROCESSES}")

    plugin = CR3Plugin(cache_dir=BENCHMARK_CACHE_DIR)

    if not plugin.is_available():
        logger.error("CR3Plugin ist nicht verfügbar. Stellen Sie sicher, dass Exiftool installiert ist.")
        return

    md5_hash = calculate_md5(TEST_CR3_FILE)
    
    # --- Benchmark für den ersten Durchlauf (ohne Cache) ---
    logger.info("\n--- Erster Durchlauf (ohne Cache) ---")
    success, run_time, view_path, thumb_path = run_single_process(plugin, TEST_CR3_FILE, md5_hash, "Erster Durchlauf")
    
    if success:
        logger.info(f"View Image: {view_path}")
        logger.info(f"Thumbnail: {thumb_path}")
    else:
        logger.error("Benchmark abgebrochen, da der erste Durchlauf fehlgeschlagen ist.")
        return # Benchmark abbrechen, wenn der erste Durchlauf fehlschlägt

    # --- Benchmark für nachfolgende Durchläufe (mit Cache) ---
    logger.info(f"\n--- {NUM_RUNS} nachfolgende Durchläufe (mit Cache) ---")
    total_cached_time = 0
    for i in range(NUM_RUNS):
        success, run_time, _, _ = run_single_process(plugin, TEST_CR3_FILE, md5_hash, f"Durchlauf {i+1}")
        total_cached_time += run_time
        if not success:
            logger.error(f"Durchlauf {i+1} fehlgeschlagen.")

    avg_cached_time = total_cached_time / NUM_RUNS
    logger.info(f"\nDurchschnittliche Zeit für {NUM_RUNS} Cache-Durchläufe: {avg_cached_time:.4f} Sekunden")

    # --- Benchmark für das Löschen des Caches und erneuten Durchlauf ---
    logger.info("\n--- Cache löschen und erneut verarbeiten ---")
    import shutil
    shutil.rmtree(BENCHMARK_CACHE_DIR)
    os.makedirs(BENCHMARK_CACHE_DIR)

    success, run_time, _, _ = run_single_process(plugin, TEST_CR3_FILE, md5_hash, "Durchlauf nach Cache-Löschung")
    if not success:
        logger.error("Durchlauf nach Cache-Löschung fehlgeschlagen.")

    # --- Benchmark für parallele Verarbeitung (Simulation) ---
    logger.info(f"\n--- Starte parallele Verarbeitung von {NUM_PARALLEL_PROCESSES} Bildern ---")
    logger.info("Hinweis: Dies simuliert die parallele Verarbeitung auf Anwendungsebene.")
    logger.info("Die Plugins selbst sind synchron, aber mehrere Instanzen können parallel laufen.")

    # Erstelle eine Liste von "Dummy"-Dateien, um parallele Verarbeitung zu simulieren
    # In einer echten Anwendung wären dies verschiedene Dateien
    simulated_files = []
    for i in range(NUM_PARALLEL_PROCESSES):
        # Erstelle eine temporäre Kopie der Testdatei, um separate Cache-Einträge zu erzwingen
        # und die Simulation realistischer zu machen (jeder "Prozess" arbeitet an einer "neuen" Datei)
        temp_file_path = os.path.join(BENCHMARK_CACHE_DIR, f"simulated_file_{i}.cr3")
        shutil.copy(TEST_CR3_FILE, temp_file_path)
        simulated_files.append((temp_file_path, calculate_md5(temp_file_path)))

    # Leere den Cache vor dem Parallel-Benchmark, um Kaltstarts zu simulieren
    shutil.rmtree(BENCHMARK_CACHE_DIR)
    os.makedirs(BENCHMARK_CACHE_DIR)

    start_parallel_time = time.time()
    with ThreadPoolExecutor(max_workers=NUM_PARALLEL_PROCESSES) as executor:
        futures = []
        for i, (file_path, file_md5) in enumerate(simulated_files):
            # Jede Future erhält eine eigene Plugin-Instanz, um Isolation zu gewährleisten
            # In einer echten Anwendung würde man vielleicht einen Pool von Plugins verwalten
            # oder sicherstellen, dass die Plugin-Instanz thread-sicher ist.
            # Für diesen Benchmark ist eine neue Instanz pro Task am einfachsten.
            task_plugin = CR3Plugin(cache_dir=BENCHMARK_CACHE_DIR)
            futures.append(executor.submit(run_single_process, task_plugin, file_path, file_md5, f"Paralleler Durchlauf {i+1}"))
        
        for future in as_completed(futures):
            success, run_time, _, _ = future.result()
            if not success:
                logger.error("Ein paralleler Durchlauf ist fehlgeschlagen.")

    end_parallel_time = time.time()
    logger.info(f"\nGesamtzeit für {NUM_PARALLEL_PROCESSES} parallele Durchläufe: {end_parallel_time - start_parallel_time:.4f} Sekunden")
    logger.info(f"Dies ist die Wanduhrzeit, die vergeht, während alle {NUM_PARALLEL_PROCESSES} Aufgaben ausgeführt werden.")

    # Aufräumen des Benchmark-Cache-Verzeichnisses
    logger.info(f"\nBereinige Benchmark-Cache-Verzeichnis: {BENCHMARK_CACHE_DIR}")
    shutil.rmtree(BENCHMARK_CACHE_DIR)

if __name__ == "__main__":
    run_benchmark()
