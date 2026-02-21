# antrian-uii

Script Python untuk memantau antrian RS UII via Telegram bot.

## Analisis cara mendapatkan data antrian

Hasil inspeksi terhadap `https://antrian.rsuii.co.id/` menunjukkan:

1. Halaman memakai **ASP.NET WebForms** dengan form `id="frm"` dan hidden fields (`__VIEWSTATE`, `__EVENTVALIDATION`, dll).
2. Daftar poli ada di `<select id="ddUNIT">`.
3. Daftar dokter/praktek ada di `<select id="ddDaftarDokter">`.
4. Data antrian muncul setelah postback `__EVENTTARGET=ddDaftarDokter`.
5. Informasi penting:
   - Total antrian: `#lblTotal`
   - Antrian saat ini: `#lblCurrent`
   - Antrian Selanjutnya / Dilewati / Selesai: deretan elemen `<h1>` di bawah judul section.
6. Tanda `*` pada nomor di section **Antrian Selanjutnya** berarti **belum check-in**. Tanpa `*` berarti **sudah check-in**.

Implementasi script mengikuti alur HTTP ini:

- `GET /` untuk ambil hidden fields + opsi awal.
- `POST /` dengan `__EVENTTARGET=ddUNIT` untuk load daftar dokter sesuai poli.
- `POST /` dengan `__EVENTTARGET=ddDaftarDokter` untuk menampilkan data antrian dokter terpilih.

## Fitur bot

- `/start`:
  - pilih poli dari daftar,
  - pilih dokter/praktek dari daftar,
  - masukkan nomor antrian.
- Background check periodik (default tiap 60 detik).
- Menghitung:
  - jumlah sudah check-in (tanpa `*`),
  - jumlah belum check-in (dengan `*`),
  - sisa antrian tercepat (asumsi semua yang sudah check-in sebelum nomor kita akan dipanggil dulu),
  - sisa antrian terlama (asumsi semua nomor sebelum nomor kita dipanggil dulu).
- Kirim notifikasi:
  - setiap ada perubahan data, atau
  - paksa heartbeat tiap 10 menit (configurable).

## Menjalankan

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="<token-bot-anda>"
python monitor_bot.py
```

## Konfigurasi env

- `TELEGRAM_BOT_TOKEN` (wajib)
- `POLL_SECONDS` (default `60`)
- `NOTIFY_FORCE_SECONDS` (default `600`)
- `DATA_FILE` (default `subscriptions.json`)
- `LOG_LEVEL` (default `INFO`)

## Command bot

- `/start` mulai setup monitoring
- `/status` cek status saat ini
- `/stop` hentikan monitoring
