from pathlib import Path

import gradio as gr
import torch
import os
import json

from demo import YMCA

if __name__ == "__main__":
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    servicer = YMCA(device)
    EXAMPLE_LIST = servicer.get_example_list()
    css = servicer.get_css()

    with gr.Blocks(theme='nota-ai/theme', css=css) as demo:
        header = gr.Markdown(Path('docs/header.md').read_text())
        gr.Markdown(Path('docs/description.md').read_text())
        with gr.Row():
            with gr.Column():
                video = gr.Video(label="Upload a video file", height=400)
                text = gr.Textbox(label="Caption of Selected Video", max_lines=5, placeholder="Enter your captions")
                inference_button = gr.Button(value="Inference with YMCA", variant="primary")

                with gr.Tab("Example Videos"):
                    examples = gr.Examples(examples=EXAMPLE_LIST, inputs=[video, text], outputs=[video, text])
            with gr.Column():
                result = gr.Label()
    
        inputs = [video, text]

        # Click the button for inference
        inference_outputs = [result]
        inference_button.click(servicer.inference, inputs=inputs, outputs=inference_outputs)

demo.launch()