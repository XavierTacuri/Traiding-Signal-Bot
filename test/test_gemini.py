import asyncio
import google.generativeai as genai

# PON AQUÍ TU API KEY
API_KEY = "AIzaSyCtA5l_l4ZI0yDxZ_ojs42sZRb9jr_wm7Q"

genai.configure(api_key=API_KEY)

model = genai.GenerativeModel("gemini-2.0-flash")


async def main():
    try:
        response = await model.generate_content_async(
            "Responde solo con la palabra OK"
        )

        print("RESPUESTA GEMINI:")
        print(response.text)

    except Exception as e:
        print("ERROR GEMINI:")
        print(e)


asyncio.run(main())