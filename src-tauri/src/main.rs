// Tauri tarafından üretilen çerçeveyi başlatır.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    ps_editor_lib::run()
}
