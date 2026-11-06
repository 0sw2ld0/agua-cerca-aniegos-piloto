import Foundation
import PDFKit
for ruta in CommandLine.arguments.dropFirst() {
    guard let doc = PDFDocument(url: URL(fileURLWithPath: ruta)) else { print("NO SE PUDO ABRIR", ruta); continue }
    print("##### \(ruta) · \(doc.pageCount) páginas")
    print(doc.string ?? "")
}
