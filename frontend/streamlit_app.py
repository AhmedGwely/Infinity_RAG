"""Streamlit UI entry point; upload and chat workflows are a later milestone."""


def main() -> None:
    """Render the current UI scaffold without implying RAG is implemented."""
    try:
        import streamlit as st
    except ImportError as error:
        raise RuntimeError("Install project dependencies before starting Streamlit.") from error

    st.set_page_config(page_title="Infinity Egypt R&D Assistant", layout="wide")
    st.title("Infinity Egypt | R&D Knowledge Assistant")
    st.info(
        "Architecture scaffold only. PDF upload, indexing, retrieval, and answer generation "
        "will be implemented in the next reviewed milestone."
    )


if __name__ == "__main__":
    main()
