# Stremio Dailymotion Addon

This is an **unofficial** addon for Stremio that lets you search for public videos on Dailymotion and play them in Stremio.

## Usage

1. Clone or upload this repo to your GitHub account.
2. Deploy on [Render](https://render.com) as a new Web Service (Node.js).
   - Start command: `npm start`
3. Render will give you a public URL like:

   ```
   https://your-service.onrender.com/manifest.json
   ```

4. In Stremio, go to **Addons → + Add Addon → Install via URL** and paste that link.

---

⚠️ **Notes**  
- This addon uses the Dailymotion public API.  
- It returns embed URLs, so playback may vary depending on your platform.  
- For personal use only. Respect Dailymotion Terms of Service.
