def render_template(text: str, params:dict) -> str:
    out = text
    for k, v in params.items():
        out =out.replace("{{" + k + "}}", str(v))
    return out
