// Parse and extract text with the shipped PDF.js. Pixel rendering is a browser check.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
// PDF.js initializes an unused transform at import time even for text-only parsing.
globalThis.DOMMatrix=class DOMMatrix {};
const pdfjs=await import('../solar/static/vendor/pdf.min.mjs');
pdfjs.GlobalWorkerOptions.workerSrc=new URL('../solar/static/vendor/pdf.worker.min.mjs',import.meta.url).href;
const bytes=new Uint8Array(await readFile(new URL('./fixtures/preview.pdf',import.meta.url)));
const task=pdfjs.getDocument({data:bytes,isEvalSupported:false,useSystemFonts:false,disableFontFace:true,standardFontDataUrl:fileURLToPath(new URL('../solar/static/vendor/pdfjs/standard_fonts/',import.meta.url)).replaceAll('\\','/')});
try {
  const pdf=await task.promise;assert.equal(pdf.numPages,2);
  for(const [number,expected] of [[1,'Solar PDF preview'],[2,'Second page']]){
    const page=await pdf.getPage(number),text=await page.getTextContent();assert(text.items.some(item=>item.str===expected));
  }
  console.log('PDF.js parser passed: two-page document, page navigation data, and text extraction. Canvas rendering still requires browser acceptance.');
}finally{await task.destroy();}
